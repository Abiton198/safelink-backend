"""One Firestore document per person, with everything about them under it.

THE STRUCTURE
-------------
    users/{name}__{uid}                 <- the person's ROOT document
    |    uid, user_ref, slug, display_name, email, created_at, updated_at,
    |    schema_version
    |    subscription: { ... }          (map: plan, status, PayFast token, period dates)
    |    onboarding:   { ... }          (map: step, saved, skipped, started, done)
    |
    +-- profile/main                    emergency profile (name, DOB hash, notes)
    +-- contacts/{contactId}            emergency contacts
    +-- vehicles/{vehicleId}            vehicles + their photo list
    +-- tags/{qrId}                     the person's claimed SafeLink tags
    +-- payments/{paymentId}            PayFast payment notifications
    +-- orders/{orderId}                prepaid (non-card) payment orders

Open one document in the Firestore console and every piece of that person's
data is underneath it. Nothing about a person lives anywhere else, except the
two top-level collections explained at the bottom.

THE DOCUMENT NAME  ("jane-doe__DUCoM9ZE2iYWhhnEXRLGFO2gFeD3")
--------------------------------------------------------------
  name part  a readable slug of the person's name (or email) at the moment
             the document was FIRST created, so you can spot people in the
             console. It is frozen: it never changes, even if they later
             rename themselves. The current name is the `display_name` field.
  uid part   their Firebase uid, which keeps the name unique.

HOW THE APP FINDS A PERSON'S DOCUMENT
-------------------------------------
Because the name part is frozen text, the app cannot rebuild the document name
from the uid alone. It looks it up ONCE with the `uid` field
(`where uid == ...`) and remembers the answer for the life of the server
process (`_ref_cache`), so normal requests do no extra read. The mapping can
never change, so the cache can never be wrong.

TWO COLLECTIONS STAY AT THE TOP LEVEL, ON PURPOSE
--------------------------------------------------
  qr_devices/{sha256(token)}      A scan arrives with only a QR token and no
        idea who owns it, so tags need a registry that can be looked up by
        the token's hash. It also holds tags that nobody has claimed yet. It
        stores only routing and safety data: owner_uid, owner_ref (this
        person's document name), active, sold, tag_ref and the lock-out
        counters. The owner's own view of the tag is users/.../tags/{qrId}.
  payment_webhook_events          Public PayFast events that arrive with no
        user attached; kept as a log.
"""

import logging
import re
import unicodedata
from datetime import datetime, timezone

from fastapi import HTTPException
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter

from app.core.firebase import db

logger = logging.getLogger("uvicorn.error")

USERS = "users"
SCHEMA_VERSION = 2
PROFILE_DOC_ID = "main"

# uid -> document name. Safe to cache forever: a person's document name never changes.
_ref_cache: dict[str, str] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def make_slug(name: str = "", email: str = "") -> str:
    """'Zanele Nkosi' -> 'zanele-nkosi'. Falls back to the email name, then 'user'."""
    base = (name or "").strip() or (email or "").split("@")[0]
    ascii_text = (
        unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:30].strip("-")
    return slug or "user"


def make_user_doc_id(uid: str, name: str = "", email: str = "") -> str:
    """The root document's name: '<name-slug>__<uid>'."""
    if not uid or "/" in uid:
        raise ValueError("Invalid uid.")
    return f"{make_slug(name, email)}__{uid}"


# ---------------------------------------------------------------------------
# Finding and creating a person's root document
# ---------------------------------------------------------------------------
def find_user_ref(uid: str):
    """The person's root document reference, or None if they have no data yet.

    Use this for READS: it never creates anything.
    """
    cached = _ref_cache.get(uid)
    if cached:
        return db.collection(USERS).document(cached)

    docs = list(
        db.collection(USERS).where(filter=FieldFilter("uid", "==", uid)).limit(2).stream()
    )
    if not docs:
        return None

    if len(docs) > 1:
        # Should never happen. If it does, always use the same one so data
        # does not scatter further, and shout so it gets merged by hand.
        logger.error("Several user documents exist for uid %s: merge them.", uid)

    chosen = min(docs, key=lambda d: d.id)
    _ref_cache[uid] = chosen.id
    return chosen.reference


def ensure_user_ref(user: dict):
    """The person's root document reference, creating it on first use.

    `user` is the signed-in user (uid, and name / email when known).
    Use this for WRITES.
    """
    uid = user["uid"]

    existing = find_user_ref(uid)
    if existing is not None:
        return existing

    name = (user.get("name") or "").strip()
    email = (user.get("email") or "").strip()
    doc_id = make_user_doc_id(uid, name, email)
    ref = db.collection(USERS).document(doc_id)
    now = now_utc()

    try:
        ref.create(
            {
                "uid": uid,
                "user_ref": doc_id,
                "slug": make_slug(name, email),
                "display_name": name or email.split("@")[0],
                "email": email,
                "created_at": now,
                "updated_at": now,
                "schema_version": SCHEMA_VERSION,
            }
        )
    except AlreadyExists:
        pass  # a request running at the same moment created it first

    _ref_cache.pop(uid, None)
    return find_user_ref(uid) or ref


def touch_user(ref, **fields) -> None:
    """Update top-level fields of the root document (and its updated_at)."""
    ref.set({**fields, "updated_at": now_utc()}, merge=True)


# ---------------------------------------------------------------------------
# Paths under a person's root document (the ONLY place these names are written)
# ---------------------------------------------------------------------------
def profile_doc(ref):
    return ref.collection("profile").document(PROFILE_DOC_ID)


def contacts_col(ref):
    return ref.collection("contacts")


def vehicles_col(ref):
    return ref.collection("vehicles")


def tags_col(ref):
    return ref.collection("tags")


def payments_col(ref):
    return ref.collection("payments")


def orders_col(ref):
    return ref.collection("orders")


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------
def get_profile(uid: str) -> dict | None:
    """The saved emergency profile, or None (no document, or no profile yet)."""
    ref = find_user_ref(uid)
    if ref is None:
        return None
    snap = profile_doc(ref).get()
    return (snap.to_dict() or {}) if snap.exists else None


def require_profile(uid: str) -> dict:
    profile = get_profile(uid)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Emergency profile not found. Create your profile first.",
        )
    return profile


class _AbsentSnapshot:
    exists = False
    id = ""

    def to_dict(self):
        return None


class _AbsentDoc:
    """Stands in for a profile document that cannot exist yet (no user document)."""

    def get(self, *args, **kwargs):
        return _AbsentSnapshot()


def profile_ref(uid: str):
    """Drop-in for the old `profile_ref(uid)` in services/emergency.py.

    Returns the profile document, or an empty stand-in when the person has no
    data yet, so code that does `profile_ref(uid).get()` keeps working.
    """
    ref = find_user_ref(uid)
    return profile_doc(ref) if ref is not None else _AbsentDoc()
