"""Firestore storage for onboarding progress ("save and resume later").

The information the owner enters (profile, contacts, vehicles, tags) is
stored by the emergency router. This module stores only WHERE they stopped in
the wizard, so they can resume on any device:

    onboarding/{uid} = {
        step: int,            # slide they were on
        saved: [str],         # steps whose form was saved
        skipped: [str],       # steps they skipped
        started: bool,
        done: bool,           # reached the final submit
        updated_at: datetime,
    }

This is progress for the UI only. It never grants access to anything;
emergency access depends solely on the subscription.
"""

from app.core.firebase import db
from app.services.emergency import now_utc

COLLECTION = "onboarding"

DEFAULT_PROGRESS = {
    "exists": False,
    "step": 0,
    "saved": [],
    "skipped": [],
    "started": False,
    "done": False,
}


def get_progress(uid: str) -> dict:
    """Return the saved progress, or defaults if none exists yet."""
    doc = db.collection(COLLECTION).document(uid).get()

    if not doc.exists:
        return dict(DEFAULT_PROGRESS)

    data = doc.to_dict() or {}

    return {
        "exists": True,
        "step": int(data.get("step", 0)),
        "saved": list(data.get("saved", [])),
        "skipped": list(data.get("skipped", [])),
        "started": bool(data.get("started", False)),
        "done": bool(data.get("done", False)),
    }


def save_progress(
    uid: str,
    *,
    step: int,
    saved: list[str],
    skipped: list[str],
    started: bool,
    done: bool,
) -> dict:
    """Create or replace this user's progress document."""
    db.collection(COLLECTION).document(uid).set(
        {
            "step": step,
            "saved": saved,
            "skipped": skipped,
            "started": started,
            "done": done,
            "updated_at": now_utc(),
        }
    )

    return get_progress(uid)