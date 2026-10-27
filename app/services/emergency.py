"""Emergency profile, QR token, and short-lived emergency-access helpers."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from app.config import settings
from app.core.firebase import db

def now_utc():
    return datetime.now(timezone.utc)

def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def dob_digest(dob_iso: str) -> str:
    secret = settings.dob_hmac_secret
    if not secret or len(secret) < 32:
        raise HTTPException(status_code=503, detail="DOB verification is not configured on the server.")
    return hmac.new(secret.encode("utf-8"), dob_iso.encode("utf-8"), hashlib.sha256).hexdigest()

def profile_ref(uid: str):
    return db.collection("emergency_profiles").document(uid)

def require_profile(uid: str) -> dict:
    snap = profile_ref(uid).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Emergency profile not found. Create your profile first.")
    return snap.to_dict() or {}

def public_profile_view(profile: dict) -> dict:
    """Only fields deliberately approved for emergency disclosure."""
    return {
        "display_name": profile.get("display_name", ""),
        "emergency_summary": profile.get("emergency_summary", ""),
        "medical_notes": profile.get("medical_notes", ""),
        "preferred_language": profile.get("preferred_language", "en"),
    }

def create_qr_token() -> str:
    # 256-bit random bearer token. Store only its SHA-256 hash in Firestore.
    return secrets.token_urlsafe(32)

def require_valid_access_token(raw_token: str) -> dict:
    ref = db.collection("emergency_access_tokens").document(hash_secret(raw_token))
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=401, detail="Emergency access token is invalid or expired.")
    record = snap.to_dict() or {}
    expiry = record.get("expires_at")
    if not expiry or expiry <= now_utc():
        ref.delete()
        raise HTTPException(status_code=401, detail="Emergency access token is invalid or expired.")
    qr_ref = db.collection("qr_devices").document(record.get("qr_id", ""))
    qr_snap = qr_ref.get()
    if not qr_snap.exists or not (qr_snap.to_dict() or {}).get("active", False):
        raise HTTPException(status_code=403, detail="This SafeLink tag is inactive.")
    return record

def verify_qr_dob_and_issue_access(qr_hash: str, dob_iso: str) -> str:
    """Check DOB and issue a short-lived scoped token; lock after repeated failures."""
    qr_ref = db.collection("qr_devices").document(qr_hash)
    qr_snap = qr_ref.get()
    if not qr_snap.exists:
        raise HTTPException(status_code=404, detail="SafeLink tag not found.")
    qr = qr_snap.to_dict() or {}
    if not qr.get("active", False):
        raise HTTPException(status_code=410, detail="This SafeLink tag has been deactivated.")

    now = now_utc()
    lock_until = qr.get("locked_until")
    if lock_until and lock_until > now:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    profile = require_profile(qr["owner_uid"])
    expected = profile.get("dob_hmac", "")
    supplied = dob_digest(dob_iso)
    if not expected or not hmac.compare_digest(expected, supplied):
        attempts = int(qr.get("failed_attempts", 0)) + 1
        updates = {"failed_attempts": attempts, "last_failed_at": now, "updated_at": now}
        if attempts >= 5:
            updates["locked_until"] = now + timedelta(minutes=15)
            updates["failed_attempts"] = 0
        qr_ref.update(updates)
        raise HTTPException(status_code=403, detail="The date of birth could not be verified.")

    qr_ref.update({"failed_attempts": 0, "locked_until": None, "last_verified_at": now, "updated_at": now})
    raw_access = secrets.token_urlsafe(48)
    access_hash = hash_secret(raw_access)
    db.collection("emergency_access_tokens").document(access_hash).set({
        "owner_uid": qr["owner_uid"],
        "qr_id": qr_hash,
        "created_at": now,
        "expires_at": now + timedelta(minutes=10),
        "scope": "emergency_read",
    })
    return raw_access
