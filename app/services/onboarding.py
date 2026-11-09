"""Onboarding progress ("save and resume later").

WHERE THE DATA LIVES
--------------------
The `onboarding` map inside the person's root document (users/{name}__{uid}):

    onboarding: {
        step: int,          # slide they were on
        saved: [str],       # steps whose form was saved
        skipped: [str],     # steps they skipped
        started: bool,
        done: bool,         # finished (card added / subscription confirmed)
        updated_at: datetime,
    }

What the person TYPED (profile, contacts, vehicles, tags) is stored by the
emergency router. This is only WHERE they stopped in the wizard. It never
grants access to anything.
"""

from app.services import userstore

DEFAULT_PROGRESS = {
    "exists": False,
    "step": 0,
    "saved": [],
    "skipped": [],
    "started": False,
    "done": False,
}


def get_progress(uid: str) -> dict:
    """The saved progress, or defaults if none exists yet. Never creates data."""
    ref = userstore.find_user_ref(uid)
    if ref is None:
        return dict(DEFAULT_PROGRESS)

    snap = ref.get()
    data = ((snap.to_dict() or {}).get("onboarding") if snap.exists else None) or None
    if not data:
        return dict(DEFAULT_PROGRESS)

    return {
        "exists": True,
        "step": int(data.get("step", 0)),
        "saved": list(data.get("saved", [])),
        "skipped": list(data.get("skipped", [])),
        "started": bool(data.get("started", False)),
        "done": bool(data.get("done", False)),
    }


def save_progress(
    user: dict,
    *,
    step: int,
    saved: list[str],
    skipped: list[str],
    started: bool,
    done: bool,
) -> dict:
    """Save progress. `user` is the signed-in user (creates their document if new)."""
    ref = userstore.ensure_user_ref(user)

    ref.set(
        {
            "onboarding": {
                "step": step,
                "saved": saved,
                "skipped": skipped,
                "started": started,
                "done": done,
                "updated_at": userstore.now_utc(),
            },
            "updated_at": userstore.now_utc(),
        },
        merge=True,
    )

    return get_progress(user["uid"])
