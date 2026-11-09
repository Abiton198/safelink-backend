"""Authenticated owner endpoints: profile, emergency contacts, vehicles, photos, QR tags.

WHERE EVERYTHING IS STORED
--------------------------
All of a person's data sits under ONE document, users/{name}__{uid}
(see services/userstore.py for the full picture):

    profile/main         <- PUT/GET /profile/me
    contacts/{id}        <- /profile/me/contacts
    vehicles/{id}        <- /profile/me/vehicles (+ photos)
    tags/{qrId}          <- /qr-tags   (the person's view of their tags)

Because a request can only reach the signed-in person's own document, there
is no "does this belong to you?" field to check: the path IS the ownership.

Owners may SAVE or SKIP any setup step, in any order, BEFORE subscribing. So
none of these endpoints needs a subscription, and none demands that a profile
already exists. Emergency details are released to a scanner only by
public_emergency.py, which is where the subscription is enforced.
"""
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.config import settings
from app.core.auth import current_user
from app.core.firebase import db
from app.schemas.emergency import (
    ContactCreate, ContactUpdate, ProfileUpdate, QRCreate, VehicleCreate, VehicleUpdate,
)
from app.services import userstore, vehicle_images
from app.services.emergency import dob_digest

router = APIRouter(tags=["Emergency profile and QR tags"])


def clean_record(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in {"dob_hmac"}}


def vehicle_out(vehicle_id: str, data: dict) -> dict:
    """A vehicle as the browser sees it: photos become {image_id, url} only.
    Storage paths never leave the server."""
    record = {k: v for k, v in data.items() if k != "images"}
    record["vehicle_id"] = vehicle_id
    record["images"] = vehicle_images.public_images(data)
    return record


# ---------------------------------------------------------------------------
# Profile  ->  users/{id}/profile/main
# ---------------------------------------------------------------------------
@router.get("/profile/me")
def get_my_profile(user: dict = Depends(current_user)):
    profile = userstore.get_profile(user["uid"])
    if profile is None:
        # New user who has not saved a profile yet (or skipped that step).
        # A normal state during onboarding, so it is not an error.
        return {
            "owner_uid": user["uid"],
            "display_name": "",
            "emergency_summary": "",
            "medical_notes": "",
            "preferred_language": "en",
            "profile_exists": False,
        }
    return clean_record(profile)


@router.put("/profile/me")
def upsert_my_profile(body: ProfileUpdate, user: dict = Depends(current_user)):
    ref = userstore.ensure_user_ref(user)
    profile_ref = userstore.profile_doc(ref)
    existing = profile_ref.get()
    now = userstore.now_utc()
    payload = {
        "owner_uid": user["uid"],
        "display_name": body.display_name.strip(),
        "dob_hmac": dob_digest(body.date_of_birth.isoformat()),
        "emergency_summary": body.emergency_summary.strip(),
        "medical_notes": body.medical_notes.strip(),
        "preferred_language": body.preferred_language.strip() or "en",
        "updated_at": now,
        "created_at": (existing.to_dict() or {}).get("created_at", now) if existing.exists else now,
    }
    profile_ref.set(payload, merge=True)
    # Keep the readable name on the root document current (its id stays fixed).
    userstore.touch_user(ref, display_name=payload["display_name"])
    return {"message": "Emergency profile saved.", "profile": clean_record(payload)}


# ---------------------------------------------------------------------------
# Contacts  ->  users/{id}/contacts/{contactId}
# ---------------------------------------------------------------------------
@router.get("/profile/me/contacts")
def list_contacts(user: dict = Depends(current_user)):
    ref = userstore.find_user_ref(user["uid"])
    if ref is None:
        return {"contacts": []}
    records = [dict(d.to_dict() or {}, contact_id=d.id) for d in userstore.contacts_col(ref).stream()]
    return {"contacts": sorted(records, key=lambda x: (x.get("priority", 10), x.get("name", "")))}


@router.post("/profile/me/contacts", status_code=status.HTTP_201_CREATED)
def create_contact(body: ContactCreate, user: dict = Depends(current_user)):
    ref = userstore.ensure_user_ref(user)
    contact_id = uuid4().hex
    now = userstore.now_utc()
    payload = body.model_dump()
    payload.update({"created_at": now, "updated_at": now})
    userstore.contacts_col(ref).document(contact_id).set(payload)
    return {"contact_id": contact_id, **payload}


def _own_doc(collection_of, user: dict, doc_id: str, not_found: str):
    """The signed-in person's document in a sub-collection, or a 404."""
    ref = userstore.find_user_ref(user["uid"])
    if ref is None:
        raise HTTPException(status_code=404, detail=not_found)
    doc_ref = collection_of(ref).document(doc_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail=not_found)
    return doc_ref, snap


@router.patch("/profile/me/contacts/{contact_id}")
def update_contact(contact_id: str, body: ContactUpdate, user: dict = Depends(current_user)):
    doc_ref, _ = _own_doc(userstore.contacts_col, user, contact_id, "Emergency contact not found.")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied.")
    changes["updated_at"] = userstore.now_utc()
    doc_ref.update(changes)
    return {"contact_id": contact_id, **(doc_ref.get().to_dict() or {})}


@router.delete("/profile/me/contacts/{contact_id}")
def delete_contact(contact_id: str, user: dict = Depends(current_user)):
    doc_ref, _ = _own_doc(userstore.contacts_col, user, contact_id, "Emergency contact not found.")
    doc_ref.delete()
    return {"message": "Emergency contact deleted."}


# ---------------------------------------------------------------------------
# Vehicles  ->  users/{id}/vehicles/{vehicleId}   (photos: see vehicle_images.py)
# ---------------------------------------------------------------------------
@router.get("/profile/me/vehicles")
def list_vehicles(user: dict = Depends(current_user)):
    ref = userstore.find_user_ref(user["uid"])
    if ref is None:
        return {"vehicles": []}
    return {"vehicles": [vehicle_out(d.id, d.to_dict() or {}) for d in userstore.vehicles_col(ref).stream()]}


@router.post("/profile/me/vehicles", status_code=status.HTTP_201_CREATED)
def create_vehicle(body: VehicleCreate, user: dict = Depends(current_user)):
    ref = userstore.ensure_user_ref(user)
    vehicle_id = uuid4().hex
    now = userstore.now_utc()
    payload = body.model_dump()
    payload.update({"images": [], "created_at": now, "updated_at": now})
    userstore.vehicles_col(ref).document(vehicle_id).set(payload)
    return {"vehicle_id": vehicle_id, **payload}


@router.patch("/profile/me/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: str, body: VehicleUpdate, user: dict = Depends(current_user)):
    doc_ref, _ = _own_doc(userstore.vehicles_col, user, vehicle_id, "Vehicle not found.")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied.")
    changes["updated_at"] = userstore.now_utc()
    doc_ref.update(changes)
    return vehicle_out(vehicle_id, doc_ref.get().to_dict() or {})


@router.delete("/profile/me/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: str, user: dict = Depends(current_user)):
    doc_ref, snap = _own_doc(userstore.vehicles_col, user, vehicle_id, "Vehicle not found.")
    vehicle_images.delete_all_images(snap.to_dict() or {})
    doc_ref.delete()
    return {"message": "Vehicle deleted."}


@router.post("/profile/me/vehicles/{vehicle_id}/images", status_code=status.HTTP_201_CREATED)
def upload_vehicle_image(vehicle_id: str, file: UploadFile = File(...), user: dict = Depends(current_user)):
    """Add one photo (maximum 5 per vehicle). No subscription needed."""
    return vehicle_images.add_image(user["uid"], vehicle_id, file)


@router.delete("/profile/me/vehicles/{vehicle_id}/images/{image_id}")
def delete_vehicle_image(vehicle_id: str, image_id: str, user: dict = Depends(current_user)):
    vehicle_images.delete_image(user["uid"], vehicle_id, image_id)
    return {"message": "Photo deleted."}


# ---------------------------------------------------------------------------
# QR tags  ->  users/{id}/tags/{qrId}   +   qr_devices/{qrId} (registry)
#
#   users/.../tags/{qrId}   label, tag_ref, claimed_at: what the OWNER sees
#   qr_devices/{qrId}       owner_uid, owner_ref, active, lock-out counters:
#                           what a SCAN needs (a scan knows only the token)
# `active` and `last_verified_at` live only in the registry, so there is one
# source of truth for them.
# ---------------------------------------------------------------------------
@router.get("/qr-tags")
def list_qr_tags(user: dict = Depends(current_user)):
    ref = userstore.find_user_ref(user["uid"])
    if ref is None:
        return {"qr_tags": []}

    mine = list(userstore.tags_col(ref).stream())
    registry = {}
    if mine:
        registry = {
            s.id: (s.to_dict() or {})
            for s in db.get_all([db.collection("qr_devices").document(t.id) for t in mine])
            if s.exists
        }

    tags = []
    for t in mine:
        own = t.to_dict() or {}
        reg = registry.get(t.id, {})
        tags.append({
            "qr_id": t.id,
            "tag_ref": own.get("tag_ref", ""),
            "label": own.get("label", "SafeLink tag"),
            "active": reg.get("active", False),
            "created_at": own.get("claimed_at") or own.get("created_at"),
            "last_verified_at": reg.get("last_verified_at"),
        })
    return {"qr_tags": tags}


@router.post("/qr-tags", status_code=status.HTTP_201_CREATED)
def create_qr_tag(body: QRCreate, user: dict = Depends(current_user)):
    ref = userstore.ensure_user_ref(user)
    userstore.require_profile(user["uid"])
    # QR token is returned once. Only its hash is stored server-side.
    import secrets, hashlib
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = userstore.now_utc()
    label = body.label.strip()

    batch = db.batch()
    batch.set(db.collection("qr_devices").document(token_hash), {
        "owner_uid": user["uid"], "owner_ref": ref.id, "active": True, "sold": True,
        "created_at": now, "updated_at": now, "failed_attempts": 0, "locked_until": None,
    })
    batch.set(userstore.tags_col(ref).document(token_hash), {
        "qr_id": token_hash, "label": label, "tag_ref": "", "created_at": now, "claimed_at": now,
    })
    batch.commit()

    scan_url = f"{settings.frontend_base_url.rstrip('/')}/scan/{raw_token}"
    return {
        "message": "QR tag created. Save or print this QR URL now; the raw token is not stored and cannot be retrieved later.",
        "qr_id": token_hash, "label": label, "scan_url": scan_url, "active": True,
    }


@router.post("/qr-tags/{qr_id}/deactivate")
def deactivate_qr_tag(qr_id: str, user: dict = Depends(current_user)):
    tag_ref, _ = _own_doc(userstore.tags_col, user, qr_id, "QR tag not found.")
    now = userstore.now_utc()
    db.collection("qr_devices").document(qr_id).update({"active": False, "updated_at": now})
    tag_ref.update({"updated_at": now})
    return {"message": "QR tag deactivated.", "qr_id": qr_id, "active": False}
