"""Firestore-backed subscription operations and access policy.

HOW THIS ALIGNS WITH THE FRONTEND ONBOARDING
--------------------------------------------
A new owner can SAVE or SKIP every setup step (profile, contacts, vehicles,
claim tag) BEFORE subscribing. Only at the very last step does the frontend
open the PayFast gateway. So the backend rule is:

  * Owner set-up endpoints   -> NO subscription needed
        profile, contacts, vehicles, qr-tags (list/claim/deactivate),
        onboarding progress, subscription status, payments/checkout,
        subscription cancel
  * Releasing emergency data -> subscription REQUIRED
        the public QR verify / emergency endpoints (what a scanner sees)

Use `Depends(require_active_subscription)` ONLY on the second group.
Emergency access therefore switches on when the subscription is active, and
never before.

This is an application-level check. It does not replace secure Firestore
rules or verification of payment-provider notifications (PayFast ITN).
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.core.firebase import db

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
    """Convert supported Firestore/Python date values to aware UTC datetimes.

    Firestore normally returns datetime objects. ISO strings are also
    handled to make the expiry check more robust.
    """

    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            # An unrecognised expiry must not silently grant access.
            return None

    if not isinstance(value, datetime):
        return None

    if value.tzinfo is None:
        # Treat naive values as UTC rather than comparing incompatible
        # naive and timezone-aware datetime objects.
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def get_subscription(uid: str) -> dict | None:
    """Return the subscription belonging to this authenticated user."""
    doc = db.collection("subscriptions").document(uid).get()

    if not doc.exists:
        return None

    return doc.to_dict() or {}


def access_problem(subscription: dict | None) -> str | None:
    """Explain why a subscription does NOT grant access, or None if it does.

    Never raises. This is the single place that decides eligibility, used by
    both `require_active_subscription` (raises) and
    `get_subscription_summary` (reports to the frontend).
    """

    if not subscription:
        return "An active SafeLink subscription is required."

    if subscription.get("status") not in ALLOWED_STATUSES:
        return (
            "Your SafeLink subscription is not active. "
            "Please renew to continue."
        )

    period_end_value = subscription.get("current_period_end")

    if period_end_value is not None:
        period_end = normalise_datetime(period_end_value)

        # Fail closed if an expiry was set but cannot be interpreted.
        if period_end is None:
            return (
                "Your subscription expiry could not be verified. "
                "Please contact SafeLink support."
            )

        if period_end <= now_utc():
            return (
                "Your SafeLink subscription has expired. "
                "Please renew to continue."
            )

    return None


def require_active_subscription(uid: str) -> dict:
    """Reject requests unless the user's subscription is currently eligible.

    Use ONLY where emergency information is released (see module docstring).
    Do NOT use it on onboarding / set-up endpoints.
    """

    subscription = get_subscription(uid)
    problem = access_problem(subscription)

    if problem:
        raise HTTPException(status_code=402, detail=problem)

    return subscription  # type: ignore[return-value]


def get_subscription_summary(uid: str) -> dict:
    """Safe, always-successful status for GET /api/v1/subscriptions/me.

    A user who has not subscribed yet is a NORMAL state during onboarding,
    so this returns 200 with status "not_started" instead of an error.
    Only non-sensitive fields are returned (never PayFast tokens).
    """

    subscription = get_subscription(uid)

    if not subscription:
        return {
            "status": "not_started",
            "has_access": False,
            "subscription": None,
        }

    period_end = normalise_datetime(subscription.get("current_period_end"))

    return {
        "status": subscription.get("status", "not_started"),
        "has_access": access_problem(subscription) is None,
        "subscription": {
            "plan_id": subscription.get("plan_id"),
            "current_period_end": period_end.isoformat() if period_end else None,
        },
    }