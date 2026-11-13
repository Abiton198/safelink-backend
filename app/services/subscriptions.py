"""Subscription operations and access policy.

WHERE THE DATA LIVES
--------------------
A person's subscription is the `subscription` map inside their root document
(users/{name}__{uid}), so it is read together with their other account data.
See services/userstore.py for the whole structure.

ACCESS RULES
------------------------
- Owner set-up endpoints   -> NO subscription needed
        profile, contacts, vehicles, photos, tags (claim/list/deactivate),
        onboarding, subscription status, payments/checkout, cancel
- Releasing emergency data -> Active subscription OR active trial period REQUIRED
        the public QR verify / emergency endpoints (what a scanner sees)
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from app.services import userstore

# Default trial length in days for new users without a paid subscription
TRIAL_DAYS = 14

# Statuses that count as "emergency access is on".
ALLOWED_STATUSES = {
    "trialing",
    "active",
    "past_due_grace",
}


def now_utc() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def normalise_datetime(value: Any) -> datetime | None:
    """Convert supported Firestore/Python date values to aware UTC datetimes."""
    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    if not isinstance(value, datetime):
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def get_user_doc(uid: str) -> dict:
    """Return the full user root document dictionary, or empty dict if missing."""
    ref = userstore.find_user_ref(uid)
    if ref is None:
        return {}

    snap = ref.get()
    if not snap.exists:
        return {}

    return snap.to_dict() or {}


def get_subscription(uid: str) -> dict | None:
    """The `subscription` map of this user's document, or None if there is none."""
    return get_user_doc(uid).get("subscription") or None


def get_trial_end(user_doc: dict) -> datetime | None:
    """Determine when the user's free trial expires."""
    subscription = user_doc.get("subscription") or {}

    # 1. Explicit trial end on subscription map or root user document
    explicit_end = normalise_datetime(
        subscription.get("trial_ends_at") or user_doc.get("trial_ends_at")
    )
    if explicit_end is not None:
        return explicit_end

    # 2. Fallback: calculate from user account creation timestamp
    created_at = normalise_datetime(user_doc.get("created_at"))
    if created_at is not None:
        return created_at + timedelta(days=TRIAL_DAYS)

    return None


def get_trial_info(user_doc: dict) -> dict:
    """Return trial status and exact countdown details for the frontend."""
    subscription = user_doc.get("subscription") or {}
    status = subscription.get("status")

    # If user already has an active paid plan, trial countdown is not needed
    if status in {"active", "past_due_grace"}:
        return {
            "is_trial": False,
            "trial_active": False,
            "trial_ends_at": None,
            "trial_days_remaining": 0,
            "trial_seconds_remaining": 0,
        }

    trial_end = get_trial_end(user_doc)
    if trial_end is None:
        return {
            "is_trial": False,
            "trial_active": False,
            "trial_ends_at": None,
            "trial_days_remaining": 0,
            "trial_seconds_remaining": 0,
        }

    remaining = trial_end - now_utc()
    seconds_left = max(0, int(remaining.total_seconds()))
    days_left = max(0, (seconds_left + 86399) // 86400)  # ceiling days remaining

    return {
        "is_trial": seconds_left > 0,
        "trial_active": seconds_left > 0,
        "trial_ends_at": trial_end.isoformat(),
        "trial_days_remaining": days_left,
        "trial_seconds_remaining": seconds_left,
    }


def update_subscription(user_ref, fields: dict) -> None:
    """Merge `fields` into the user's `subscription` map (other fields are kept)."""
    user_ref.set({"subscription": fields, "updated_at": now_utc()}, merge=True)


def mark_cancellation_requested(uid: str, reason: str | None) -> None:
    """Record that the owner asked to cancel (used by the cancel endpoint)."""
    ref = userstore.find_user_ref(uid)
    if ref is None or get_subscription(uid) is None:
        raise HTTPException(status_code=404, detail="No subscription found.")

    update_subscription(
        ref,
        {
            "cancellation_requested": True,
            "cancellation_reason": reason,
            "cancellation_requested_at": now_utc(),
        },
    )


def access_problem(subscription: dict | None, user_doc: dict | None = None) -> str | None:
    """Explain why a subscription/trial does NOT grant access, or None if it does."""
    now = now_utc()

    # 1. Check active paid or explicitly trialing subscription first
    if subscription and subscription.get("status") in ALLOWED_STATUSES:
        period_end_value = subscription.get("current_period_end")
        if period_end_value is not None:
            period_end = normalise_datetime(period_end_value)
            if period_end is None:
                return (
                    "Your subscription expiry could not be verified. "
                    "Please contact SafeLink support."
                )
            if period_end > now:
                return None
        else:
            return None

    # 2. Check if user is within their automatic free trial period
    if user_doc is not None:
        trial_info = get_trial_info(user_doc)
        if trial_info["trial_active"]:
            return None

    # 3. Neither paid subscription nor active trial
    if not subscription:
        return (
            "Your free trial period has ended. "
            "Please subscribe to keep SafeLink emergency access active."
        )

    if subscription.get("status") not in ALLOWED_STATUSES:
        return (
            "Your SafeLink subscription is not active. "
            "Please renew to continue."
        )

    return (
        "Your SafeLink subscription has expired. "
        "Please renew to continue."
    )


def require_active_subscription(uid: str) -> dict:
    """Reject requests unless the user is in a valid trial period or subscribed."""
    user_doc = get_user_doc(uid)
    subscription = user_doc.get("subscription") or None
    problem = access_problem(subscription, user_doc=user_doc)

    if problem:
        raise HTTPException(status_code=402, detail=problem)

    trial_info = get_trial_info(user_doc)
    return {
        **(subscription or {"status": "trialing"}),
        **trial_info,
    }


def get_subscription_summary(uid: str) -> dict:
    """Safe, always-successful status for GET /api/v1/subscriptions/me."""
    user_doc = get_user_doc(uid)
    subscription = user_doc.get("subscription") or None
    trial_info = get_trial_info(user_doc)
    has_access = access_problem(subscription, user_doc=user_doc) is None

    if not subscription:
        return {
            "status": "trialing" if trial_info["trial_active"] else "not_started",
            "has_access": has_access,
            "subscription": None,
            **trial_info,
        }

    period_end = normalise_datetime(subscription.get("current_period_end"))

    return {
        "status": subscription.get("status", "not_started"),
        "has_access": has_access,
        "subscription": {
            "plan_id": subscription.get("plan_id"),
            "current_period_end": period_end.isoformat() if period_end else None,
        },
        **trial_info,
    }