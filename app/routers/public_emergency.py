"""Public QR scan endpoints. Never return protected details before DOB verification.

WHERE THE DATA COMES FROM
-------------------------
  qr_devices/{sha256(token)}       found from the scanned token; says who owns
                                   the tag (owner_uid) and whether it is active
  users/{name}__{uid}/...          the owner's profile, contacts and vehicles,
                                   found through owner_uid (services/userstore.py)
"""
import hashlib
import hmac
import os
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from google.cloud.firestore_v1.base_query import FieldFilter
from app.schemas.emergency import DOBVerification
from app.core.firebase import db
from app.services import userstore
from app.services.emergency import (
    verify_qr_dob_and_issue_access,
    require_valid_access_token,
    public_profile_view,
    dob_digest,
)
from app.services.subscriptions import require_active_subscription, get_trial_info
from app.services.vehicle_images import responder_photo_urls

router = APIRouter(prefix="/public", tags=["Public QR emergency access"])

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")


class AlertContactsRequest(BaseModel):
    access_token: str
    message: str = Field(..., min_length=3, max_length=480)


class RevealNumbersRequest(BaseModel):
    access_token: str
    date_of_birth: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


async def send_via_twilio(to_number: str, body: str) -> bool:
    """Send WhatsApp or SMS alert via Twilio without revealing numbers to client."""
    formatted_to = (
        to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
    )
    formatted_from = (
        TWILIO_FROM_NUMBER
        if TWILIO_FROM_NUMBER.startswith("whatsapp:")
        else f"whatsapp:{TWILIO_FROM_NUMBER}"
    )

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        print(f"[DEV ALERT] To: {formatted_to} | Message: {body}")
        return True

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            data={"To": formatted_to, "From": formatted_from, "Body": body},
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        )
        return resp.status_code in (200, 201)


@router.post("/qr/{token}/verify")
def verify_scanned_qr(token: str, body: DOBVerification, request: Request):
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
        raise HTTPException(
            status_code=410, detail="This SafeLink tag has been deactivated."
        )

    require_active_subscription(owner_uid)

    access_token = verify_qr_dob_and_issue_access(
        qr_hash, body.date_of_birth.isoformat()
    )
    return {
        "message": "Verification successful. This access token expires in 10 minutes.",
        "access_token": access_token,
        "expires_in_seconds": 600,
    }


@router.get("/qr/{token}/status")
def tag_status(token: str):
    if len(token) < 30 or len(token) > 200:
        raise HTTPException(status_code=404, detail="SafeLink tag not found.")
    qr_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    snap = db.collection("qr_devices").document(qr_hash).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="SafeLink tag not found.")
    qr = snap.to_dict() or {}
    ready = bool(qr.get("owner_uid")) and bool(qr.get("active", False))
    return {"state": "ready" if ready else "not_activated"}


@router.get("/emergency")
def get_emergency_details(access_token: str):
    record = require_valid_access_token(access_token)
    owner_uid = record["owner_uid"]
    user_doc = userstore.find_user_ref(owner_uid)
    if user_doc is None:
        raise HTTPException(status_code=404, detail="Emergency details not found.")

    require_active_subscription(owner_uid)
    profile = userstore.require_profile(owner_uid)

    contact_docs = (
        userstore.contacts_col(user_doc)
        .where(filter=FieldFilter("share_with_responder", "==", True))
        .stream()
    )

    contacts = []
    for d in contact_docs:
        c = d.to_dict() or {}
        raw_phone = c.get("phone", "")
        masked_phone = f"•••• {raw_phone[-4:]}" if len(raw_phone) >= 4 else "••••"
        contacts.append(
            {
                "name": c.get("name", ""),
                "relationship": c.get("relationship", ""),
                "phone_masked": masked_phone,
                "priority": c.get("priority", 10),
            }
        )

    contacts.sort(key=lambda c: (c["priority"], c["name"]))

    vehicles = []
    for doc in userstore.vehicles_col(user_doc).stream():
        v = doc.to_dict() or {}
        vehicles.append(
            {
                "label": v.get("label", ""),
                "make": v.get("make", ""),
                "model": v.get("model", ""),
                "colour": v.get("colour", ""),
                "registration_number": v.get("registration_number", ""),
                "tracker_provider": v.get("tracker_provider", ""),
                "tracker_reference": v.get("tracker_reference", ""),
                "notes": v.get("notes", ""),
                "photos": responder_photo_urls(v),
            }
        )

    full_user_dict = user_doc.get().to_dict() or {}
    trial_info = get_trial_info(full_user_dict)

    return {
        "profile": public_profile_view(profile),
        "emergency_contacts": contacts,
        "vehicles": vehicles,
        "access_expires_at": record["expires_at"].isoformat(),
        "emergency_number_note": "If there is immediate danger, contact local emergency services.",
        **trial_info,
    }


@router.post("/emergency/alert-contacts")
async def alert_priority_contacts(payload: AlertContactsRequest):
    """Dispatch emergency message via Twilio to all priority contacts without revealing numbers."""
    record = require_valid_access_token(payload.access_token)
    owner_uid = record["owner_uid"]
    require_active_subscription(owner_uid)

    user_ref = userstore.find_user_ref(owner_uid)
    if user_ref is None:
        raise HTTPException(status_code=404, detail="User not found.")

    contact_docs = (
        userstore.contacts_col(user_ref)
        .where(filter=FieldFilter("share_with_responder", "==", True))
        .stream()
    )

    all_contacts = [d.to_dict() or {} for d in contact_docs]
    priority_contacts = sorted(
        [c for c in all_contacts if c.get("phone")],
        key=lambda c: (c.get("priority", 99), c.get("name", "")),
    )

    if not priority_contacts:
        raise HTTPException(
            status_code=404, detail="No priority contacts with phone numbers found."
        )

    profile = userstore.require_profile(owner_uid)
    owner_name = profile.get("display_name", "SafeLink User")
    alert_text = (
        f"SAFELINK EMERGENCY ALERT for {owner_name}: {payload.message.strip()}"
    )

    sent_count = 0
    for contact in priority_contacts:
        success = await send_via_twilio(contact["phone"], alert_text)
        if success:
            sent_count += 1

    if sent_count == 0:
        raise HTTPException(
            status_code=502, detail="Failed to dispatch emergency alerts."
        )

    return {
        "ok": True,
        "alerted_count": sent_count,
        "detail": f"Emergency alert successfully sent to {sent_count} priority contact(s).",
    }


@router.post("/emergency/reveal-numbers")
def reveal_contact_numbers(payload: RevealNumbersRequest):
    """Verify the owner's masked DOB passcode before releasing raw phone numbers."""
    record = require_valid_access_token(payload.access_token)
    owner_uid = record["owner_uid"]
    require_active_subscription(owner_uid)

    profile = userstore.require_profile(owner_uid)
    stored_hmac = profile.get("dob_hmac", "")
    candidate_hmac = dob_digest(payload.date_of_birth)

    if not stored_hmac or not hmac.compare_digest(stored_hmac, candidate_hmac):
        raise HTTPException(
            status_code=403, detail="Incorrect date of birth passcode."
        )

    user_ref = userstore.find_user_ref(owner_uid)
    if user_ref is None:
        raise HTTPException(status_code=404, detail="User not found.")

    contact_docs = (
        userstore.contacts_col(user_ref)
        .where(filter=FieldFilter("share_with_responder", "==", True))
        .stream()
    )

    contacts = [d.to_dict() or {} for d in contact_docs]
    contacts.sort(key=lambda c: (c.get("priority", 10), c.get("name", "")))

    numbers_map = {
        str(idx): contact["phone"]
        for idx, contact in enumerate(contacts)
        if contact.get("phone")
    }

    return {"numbers": numbers_map}