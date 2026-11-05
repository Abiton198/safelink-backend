"""Claiming a physical SafeLink tag by scanning it (no serial numbers).

How it fits together
--------------------
* The business creates a batch of UNCLAIMED tags (scripts/generate_tags.py).
  Each is a record in `qr_devices` with no `owner_uid`, plus the QR address
  `<frontend>/scan/<token>` that is printed on the tag. Only the SHA-256 hash
  of the token is stored.
* The customer scans the tag. The app reads the token from the QR code and
  asks us to LOOK IT UP: we answer with the tag's details (reference, name)
  so the form can be filled in automatically. Nothing is changed.
* The customer presses Confirm. We then LINK the tag to their account and
  switch it on, inside a transaction so two people can never both claim it.

Security notes
--------------
* Scanning proves the person can see the QR code on the tag. Once a tag has
  an owner it can never be claimed again, and reading its data still needs
  the owner's date of birth (see public_emergency.py).
* Set TAG_CLAIM_REQUIRES_SOLD=true so only tags the business has marked as
  sold can be claimed. That stops someone claiming a tag from a shop shelf.
* A missing, unknown or already-claimed tag gets a clear message but never
  reveals WHO owns it.
"""

import hashlib

from fastapi import HTTPException
from google.cloud import firestore

from app.config import settings
from app.core.firebase import db
from app.services.emergency import now_utc

COLLECTION = "qr_devices"


def token_hash(token: str) -> str:
    """The record id of a tag: SHA-256 of the secret token in its QR code."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token_shape(token: str) -> bool:
    return isinstance(token, str) and 30 <= len(token) <= 200


def decide_claim(data: dict | None, uid: str, requires_sold: bool) -> str:
    """Pure decision about a tag record (easy to test, no database).

    Returns one of:
      not_found    no such tag
      yours        already linked to THIS user
      claimed      linked to someone else
      unavailable  has no owner but may not be claimed yet (not marked sold)
      available    can be claimed now
    """
    if data is None:
        return "not_found"

    owner = data.get("owner_uid")
    if owner:
        return "yours" if owner == uid else "claimed"

    if requires_sold and not data.get("sold"):
        return "unavailable"

    return "available"


_PROBLEMS = {
    "not_found": (404, "This QR code is not a SafeLink tag."),
    "claimed": (409, "This tag has already been claimed by another account."),
    "unavailable": (
        409,
        "This tag is not available to claim yet. Please contact SafeLink support.",
    ),
}


def _requires_sold() -> bool:
    return bool(getattr(settings, "tag_claim_requires_sold", False))


def _raise_for(state: str) -> None:
    if state in _PROBLEMS:
        status, message = _PROBLEMS[state]
        raise HTTPException(status_code=status, detail=message)


def lookup(uid: str, token: str) -> dict:
    """Details to auto-fill the claim form. Changes nothing."""
    if not valid_token_shape(token):
        raise HTTPException(status_code=404, detail=_PROBLEMS["not_found"][1])

    qr_id = token_hash(token)
    snap = db.collection(COLLECTION).document(qr_id).get()
    data = (snap.to_dict() or {}) if snap.exists else None

    state = decide_claim(data, uid, _requires_sold())
    _raise_for(state)

    return {
        "state": state,  # "available" or "yours"
        "qr_id": qr_id,
        "tag_ref": data.get("tag_ref", ""),
        "label": data.get("label") or "SafeLink tag",
    }


@firestore.transactional
def _link_tag(transaction, ref, uid: str, label: str, requires_sold: bool):
    """Link the tag to the user, unless someone got there first."""
    snap = ref.get(transaction=transaction)
    data = (snap.to_dict() or {}) if snap.exists else None

    state = decide_claim(data, uid, requires_sold)
    if state != "available":
        return state, data

    now = now_utc()
    changes = {
        "owner_uid": uid,
        "label": label or data.get("label") or "SafeLink tag",
        "active": True,
        "claimed_at": now,
        "updated_at": now,
        "failed_attempts": 0,
        "locked_until": None,
    }
    transaction.update(ref, changes)
    return "claimed_now", {**data, **changes}


def confirm(uid: str, token: str, label: str) -> dict:
    """Links the scanned tag to this user and switches it on."""
    if not valid_token_shape(token):
        raise HTTPException(status_code=404, detail=_PROBLEMS["not_found"][1])

    qr_id = token_hash(token)
    ref = db.collection(COLLECTION).document(qr_id)
    state, data = _link_tag(
        db.transaction(), ref, uid, label.strip()[:80], _requires_sold()
    )

    if state not in ("claimed_now", "yours"):
        _raise_for(state)

    return {
        "message": "Tag claimed." if state == "claimed_now" else "This tag is already yours.",
        "qr_id": qr_id,
        "tag_ref": (data or {}).get("tag_ref", ""),
        "label": (data or {}).get("label") or "SafeLink tag",
        "active": True,
    }