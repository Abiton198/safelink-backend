"""Public QR scan endpoints. Never return protected details before DOB verification."""
import hashlib
from fastapi import APIRouter, HTTPException, Request
from app.schemas.emergency import DOBVerification
from app.core.firebase import db
from app.services.emergency import (
    hash_secret, now_utc, verify_qr_dob_and_issue_access,
    require_valid_access_token, public_profile_view, require_profile,
)
from app.services.subscriptions import require_active_subscription

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
    if not qr.get("active", False):
        raise HTTPException(status_code=410, detail="This SafeLink tag has been deactivated.")
    # Subscription is checked before a verification can release emergency data.
    require_active_subscription(qr["owner_uid"])
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
    require_active_subscription(record["owner_uid"])
    profile = require_profile(record["owner_uid"])
    contacts = db.collection("emergency_contacts").where("owner_uid", "==", record["owner_uid"]).where("share_with_responder", "==", True).stream()
    vehicles = db.collection("vehicles").where("owner_uid", "==", record["owner_uid"]).stream()
    return {
        "profile": public_profile_view(profile),
        "emergency_contacts": [
            {"name": c.get("name", ""), "relationship": c.get("relationship", ""), "phone": c.get("phone", ""), "priority": c.get("priority", 10)}
            for d in contacts for c in [d.to_dict() or {}]
        ],
        "vehicles": [
            {
                "label": (v.to_dict() or {}).get("label", ""),
                "make": (v.to_dict() or {}).get("make", ""),
                "model": (v.to_dict() or {}).get("model", ""),
                "colour": (v.to_dict() or {}).get("colour", ""),
                "registration_number": (v.to_dict() or {}).get("registration_number", ""),
                "tracker_provider": (v.to_dict() or {}).get("tracker_provider", ""),
                "tracker_reference": (v.to_dict() or {}).get("tracker_reference", ""),
                "notes": (v.to_dict() or {}).get("notes", ""),
            }
            for v in vehicles
        ],
        "access_expires_at": record["expires_at"].isoformat(),
        "emergency_number_note": "If there is immediate danger, contact local emergency services.",
    }
