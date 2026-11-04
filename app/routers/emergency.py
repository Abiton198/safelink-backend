"""Authenticated owner endpoints for profile, emergency contacts, vehicles, and QR tags.

ALIGNMENT WITH THE FRONTEND ONBOARDING
--------------------------------------
Owners may SAVE or SKIP any setup step, in any order, BEFORE subscribing.
So none of these endpoints needs a subscription, and none of them may demand
that a profile already exists:

  * GET /profile/me returns an empty profile for a brand-new user
    (instead of a 404 error).
  * Listing and adding contacts / vehicles works even if the profile step
    was skipped.

Emergency information is only RELEASED to a scanner by public_emergency.py,
which is where the subscription is enforced.
"""
from uuid import uuid4
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from app.core.auth import current_user
from app.core.firebase import db
from app.schemas.emergency import ProfileUpdate, ContactCreate, ContactUpdate, VehicleCreate, VehicleUpdate, QRCreate
from app.services.emergency import profile_ref, require_profile, dob_digest, now_utc
from app.config import settings
from app.services import vehicle_images

router = APIRouter(tags=["Emergency profile and QR tags"])

def owner_uid(user: dict) -> str:
    return user["uid"]

def owner_guard(user: dict) -> str:
    """Allow profile setup and editing before the user subscribes."""
    return user["uid"]

def clean_record(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in {"dob_hmac"}}

def vehicle_out(vehicle_id: str, data: dict) -> dict:
    """A vehicle as the browser sees it: photos become {image_id, url} only.
    Storage paths never leave the server."""
    record = {k: v for k, v in data.items() if k != "images"}
    record["vehicle_id"] = vehicle_id
    record["images"] = vehicle_images.public_images(data)
    return record

@router.get("/profile/me")
def get_my_profile(user: dict = Depends(current_user)):
    uid = owner_guard(user)
    snap = profile_ref(uid).get()
    if not snap.exists:
        # New user who has not saved a profile yet (or skipped that step).
        # This is a normal state during onboarding, so it is not an error.
        return {
            "owner_uid": uid,
            "display_name": "",
            "emergency_summary": "",
            "medical_notes": "",
            "preferred_language": "en",
            "profile_exists": False,
        }
    return clean_record(snap.to_dict() or {})

@router.put("/profile/me")
def upsert_my_profile(body: ProfileUpdate, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    ref = profile_ref(uid)
    existing = ref.get()
    now = now_utc()
    dob_iso = body.date_of_birth.isoformat()
    payload = {
        "owner_uid": uid,
        "display_name": body.display_name.strip(),
        "dob_hmac": dob_digest(dob_iso),
        "emergency_summary": body.emergency_summary.strip(),
        "medical_notes": body.medical_notes.strip(),
        "preferred_language": body.preferred_language.strip() or "en",
        "updated_at": now,
        "created_at": (existing.to_dict() or {}).get("created_at", now) if existing.exists else now,
    }
    ref.set(payload, merge=True)
    return {"message": "Emergency profile saved.", "profile": clean_record(payload)}

@router.get("/profile/me/contacts")
def list_contacts(user: dict = Depends(current_user)):
    uid = owner_guard(user)
    # No profile required: contacts can be added before the profile step.
    docs = db.collection("emergency_contacts").where("owner_uid", "==", uid).stream()
    records = [dict(d.to_dict() or {}, contact_id=d.id) for d in docs]
    return {"contacts": sorted(records, key=lambda x: (x.get("priority", 10), x.get("name", "")))}

@router.post("/profile/me/contacts", status_code=status.HTTP_201_CREATED)
def create_contact(body: ContactCreate, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    # No profile required (the profile step may have been skipped).
    contact_id = uuid4().hex
    payload = body.model_dump()
    payload.update({"owner_uid": uid, "created_at": now_utc(), "updated_at": now_utc()})
    db.collection("emergency_contacts").document(contact_id).set(payload)
    return {"contact_id": contact_id, **payload}

@router.patch("/profile/me/contacts/{contact_id}")
def update_contact(contact_id: str, body: ContactUpdate, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    ref = db.collection("emergency_contacts").document(contact_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("owner_uid") != uid:
        raise HTTPException(status_code=404, detail="Emergency contact not found.")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied.")
    changes["updated_at"] = now_utc()
    ref.update(changes)
    return {"contact_id": contact_id, **(ref.get().to_dict() or {})}

@router.delete("/profile/me/contacts/{contact_id}")
def delete_contact(contact_id: str, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    ref = db.collection("emergency_contacts").document(contact_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("owner_uid") != uid:
        raise HTTPException(status_code=404, detail="Emergency contact not found.")
    ref.delete()
    return {"message": "Emergency contact deleted."}

@router.get("/profile/me/vehicles")
def list_vehicles(user: dict = Depends(current_user)):
    uid = owner_guard(user)
    # No profile required: vehicles can be added before the profile step.
    docs = db.collection("vehicles").where("owner_uid", "==", uid).stream()
    return {"vehicles": [vehicle_out(d.id, d.to_dict() or {}) for d in docs]}

@router.post("/profile/me/vehicles", status_code=status.HTTP_201_CREATED)
def create_vehicle(body: VehicleCreate, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    # No profile required (the profile step may have been skipped).
    vehicle_id = uuid4().hex
    payload = body.model_dump()
    payload.update({"owner_uid": uid, "images": [], "created_at": now_utc(), "updated_at": now_utc()})
    db.collection("vehicles").document(vehicle_id).set(payload)
    return {"vehicle_id": vehicle_id, **payload}

@router.patch("/profile/me/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: str, body: VehicleUpdate, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    ref = db.collection("vehicles").document(vehicle_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("owner_uid") != uid:
        raise HTTPException(status_code=404, detail="Vehicle not found.")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied.")
    changes["updated_at"] = now_utc()
    ref.update(changes)
    return vehicle_out(vehicle_id, ref.get().to_dict() or {})

@router.delete("/profile/me/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: str, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    ref = db.collection("vehicles").document(vehicle_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("owner_uid") != uid:
        raise HTTPException(status_code=404, detail="Vehicle not found.")
    vehicle_images.delete_all_images(snap.to_dict() or {})
    ref.delete()
    return {"message": "Vehicle deleted."}

@router.post("/profile/me/vehicles/{vehicle_id}/images", status_code=status.HTTP_201_CREATED)
def upload_vehicle_image(vehicle_id: str, file: UploadFile = File(...), user: dict = Depends(current_user)):
    """Add one photo (maximum 5 per vehicle). No subscription needed."""
    uid = owner_guard(user)
    return vehicle_images.add_image(uid, vehicle_id, file)

@router.delete("/profile/me/vehicles/{vehicle_id}/images/{image_id}")
def delete_vehicle_image(vehicle_id: str, image_id: str, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    vehicle_images.delete_image(uid, vehicle_id, image_id)
    return {"message": "Photo deleted."}

@router.get("/qr-tags")
def list_qr_tags(user: dict = Depends(current_user)):
    uid = owner_guard(user)
    docs = db.collection("qr_devices").where("owner_uid", "==", uid).stream()
    tags = []
    for d in docs:
        data = d.to_dict() or {}
        tags.append({
            "qr_id": d.id, "label": data.get("label", "SafeLink tag"),
            "active": data.get("active", False), "created_at": data.get("created_at"),
            "last_verified_at": data.get("last_verified_at"),
        })
    return {"qr_tags": tags}

@router.post("/qr-tags", status_code=status.HTTP_201_CREATED)
def create_qr_tag(body: QRCreate, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    require_profile(uid)
    # QR token is returned once. Only its hash is stored server-side.
    import secrets, hashlib
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = now_utc()
    db.collection("qr_devices").document(token_hash).set({
        "owner_uid": uid, "label": body.label.strip(), "active": True,
        "created_at": now, "updated_at": now, "failed_attempts": 0,
        "locked_until": None,
    })
    scan_url = f"{settings.frontend_base_url.rstrip('/')}/scan/{raw_token}"
    return {
        "message": "QR tag created. Save or print this QR URL now; the raw token is not stored and cannot be retrieved later.",
        "qr_id": token_hash, "label": body.label.strip(), "scan_url": scan_url,
        "active": True,
    }

@router.post("/qr-tags/{qr_id}/deactivate")
def deactivate_qr_tag(qr_id: str, user: dict = Depends(current_user)):
    uid = owner_guard(user)
    ref = db.collection("qr_devices").document(qr_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("owner_uid") != uid:
        raise HTTPException(status_code=404, detail="QR tag not found.")
    ref.update({"active": False, "updated_at": now_utc()})
    return {"message": "QR tag deactivated.", "qr_id": qr_id, "active": False}