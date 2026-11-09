"""Claiming a physical SafeLink tag by scanning it (no serial numbers).

WHERE THE DATA LIVES
--------------------
  qr_devices/{sha256(token)}        the global REGISTRY (a scan knows only the
                                    token, so tags must be findable by hash).
                                    Holds tag_ref, sold, owner_uid, owner_ref,
                                    active and the lock-out counters.
  users/{name}__{uid}/tags/{qrId}   the OWNER's record of the tag (label,
                                    tag_ref, claimed_at), created on claim.

How it fits together
--------------------
* The business creates UNCLAIMED tags (scripts/generate_tags.py): registry
  records with no owner. Only the hash of each token is stored.
* The customer scans the tag. The app reads the token from the QR code and
  asks us to LOOK IT UP: we answer with the tag's details so the form can be
  filled in automatically. Nothing is changed.
* The customer presses Confirm. We LINK the tag to their account and switch it
  on, inside a transaction so two people can never both claim it.

Security notes
--------------
* Scanning proves the person can see the QR code on the tag. Once a tag has an
  owner it can never be claimed again, and reading its data still needs the
  owner's date of birth (see public_emergency.py).
* Set TAG_CLAIM_REQUIRES_SOLD=true so only tags the business has marked as
  sold can be claimed.
* A missing, unknown or already-claimed tag gets a clear message but never
  reveals WHO owns it.
"""

import hashlib

from fastapi import HTTPException
from google.cloud import firestore

from app.config import settings
from app.core.firebase import db
from app.services import userstore

COLLECTION = "qr_devices"


def token_hash(token: str) -> str:
    """The registry id of a tag: SHA-256 of the secret token in its QR code."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token_shape(token: str) -> bool:
    return isinstance(token, str) and 30 <= len(token) <= 200


def decide_claim(data: dict | None, uid: str, requires_sold: bool) -> str:
    """Pure decision about a registry record (easy to test, no database).

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
def _link_tag(transaction, registry_ref, tag_doc_ref, uid, owner_ref_id, label, requires_sold):
    """Link the tag to the user, unless someone got there first."""
    snap = registry_ref.get(transaction=transaction)
    data = (snap.to_dict() or {}) if snap.exists else None

    # All reads first, then writes (a Firestore transaction rule).
    tag_snap = tag_doc_ref.get(transaction=transaction)
    state = decide_claim(data, uid, requires_sold)
    now = userstore.now_utc()

    if state == "yours":
        # Already theirs. Never overwrite a name they chose; only make sure
        # their own record of the tag exists, and apply a new name if one was typed.
        own = (tag_snap.to_dict() or {}) if tag_snap.exists else {}
        if not tag_snap.exists:
            transaction.set(tag_doc_ref, {
                "qr_id": registry_ref.id,
                "tag_ref": data.get("tag_ref", ""),
                "label": label or data.get("label") or "SafeLink tag",
                "claimed_at": data.get("claimed_at") or now,
                "created_at": now,
            })
        elif label:
            transaction.update(tag_doc_ref, {"label": label})
        shown = label or own.get("label") or data.get("label") or "SafeLink tag"
        return "yours", {**data, "label": shown}

    if state != "available":
        return state, data

    chosen_label = label or data.get("label") or "SafeLink tag"
    registry_changes = {
        "owner_uid": uid,
        "owner_ref": owner_ref_id,   # this person's document name
        "active": True,
        "claimed_at": now,
        "updated_at": now,
        "failed_attempts": 0,
        "locked_until": None,
    }
    transaction.update(registry_ref, registry_changes)
    transaction.set(tag_doc_ref, {
        "qr_id": registry_ref.id,
        "tag_ref": data.get("tag_ref", ""),
        "label": chosen_label,
        "claimed_at": now,
        "created_at": now,
    })
    return "claimed_now", {**data, **registry_changes, "label": chosen_label}


def confirm(user: dict, token: str, label: str) -> dict:
    """Links the scanned tag to this user and switches it on."""
    if not valid_token_shape(token):
        raise HTTPException(status_code=404, detail=_PROBLEMS["not_found"][1])

    user_ref = userstore.ensure_user_ref(user)
    qr_id = token_hash(token)

    state, data = _link_tag(
        db.transaction(),
        db.collection(COLLECTION).document(qr_id),
        userstore.tags_col(user_ref).document(qr_id),
        user["uid"],
        user_ref.id,
        label.strip()[:80],
        _requires_sold(),
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
