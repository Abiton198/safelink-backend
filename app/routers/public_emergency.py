"""Public QR scan endpoints. Never return protected details before DOB verification."""
import hashlib
from fastapi import APIRouter, HTTPException, Request
from app.schemas.emergency import DOBVerification
from app.core.firebase import db
from app.services.emergency import (
    verify_qr_dob_and_issue_access,
    require_valid_access_token, public_profile_view, require_profile,
)
from app.services.subscriptions import require_active_subscription
from app.services.vehicle_images import responder_photo_urls

router = APIRouter(prefix="/public", tags=["Public QR emergency access"])

@router.post("/qr/{token}/verify")
def verify_scanned_qr(token: str, body: DOBVerification, request: Request):
    # A token is only an identifier, not sufficient to access emergency data.
    if len(token) < 30 or len(token) > 200:
        raise HTTPException(status_code=404, detail="SafeLink tag not found.")
    qr_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    qr_ref = db.collection("qr_devices").document(qr_hash)
    qr_snap = qr_ref.get()
    if not qr_snap.exists:
        raise HTTPException(status_code=404, detail="SafeLink tag not found.")
    qr = qr_snap.to_dict() or {}

    owner_uid = qr.get("owner_uid")
    if not owner_uid:
        raise HTTPException(
            status_code=409,
            detail="This SafeLink tag has not been activated by its owner yet.",
        )

    if not qr.get("active", False):
        raise HTTPException(status_code=410, detail="This SafeLink tag has been deactivated.")

    # Emergency access exists only while the owner's trial/subscription is
    # active. Checked BEFORE a verification can release anything.
    require_active_subscription(owner_uid)

    access_token = verify_qr_dob_and_issue_access(qr_hash, body.date_of_birth.isoformat())
    return {
        "message": "Verification successful. This access token expires in 10 minutes.",
        "access_token": access_token,
        "expires_in_seconds": 600,
    }

@router.get("/emergency")
def get_emergency_details(access_token: str):
    # Short-lived access token is passed as query parameter because this route is intended
    # for a simple QR-scanner web page. Avoid logging query strings at your proxy/app.
    record = require_valid_access_token(access_token)
    owner_uid = record["owner_uid"]
    require_active_subscription(owner_uid)
    profile = require_profile(owner_uid)

    contact_docs = (
        db.collection("emergency_contacts")
        .where("owner_uid", "==", owner_uid)
        .where("share_with_responder", "==", True)
        .stream()
    )
    contacts = [
        {
            "name": c.get("name", ""),
            "relationship": c.get("relationship", ""),
            "phone": c.get("phone", ""),
            "priority": c.get("priority", 10),
        }
        for d in contact_docs for c in [d.to_dict() or {}]
    ]
    # Call the highest priority (1) first.
    contacts.sort(key=lambda c: (c["priority"], c["name"]))

    vehicle_docs = db.collection("vehicles").where("owner_uid", "==", owner_uid).stream()
    vehicles = []
    for doc in vehicle_docs:
        v = doc.to_dict() or {}
        vehicles.append({
            "label": v.get("label", ""),
            "make": v.get("make", ""),
            "model": v.get("model", ""),
            "colour": v.get("colour", ""),
            "registration_number": v.get("registration_number", ""),
            "tracker_provider": v.get("tracker_provider", ""),
            "tracker_reference": v.get("tracker_reference", ""),
            "notes": v.get("notes", ""),
            # Up to 5 photos, as links that expire in 10 minutes. Never fails
            # the request: no photos is returned if storage has a problem.
            "photos": responder_photo_urls(v),
        })

    return {
        "profile": public_profile_view(profile),
        "emergency_contacts": contacts,
        "vehicles": vehicles,
        "access_expires_at": record["expires_at"].isoformat(),
        "emergency_number_note": "If there is immediate danger, contact local emergency services.",
    }