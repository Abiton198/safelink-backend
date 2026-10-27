"""Authenticated subscription status and cancellation-intent endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from app.core.auth import current_user
from app.core.firebase import db
from app.schemas.subscriptions import CancelSubscriptionRequest
from app.services.subscriptions import get_subscription, now_utc

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

@router.get("/me")
def my_subscription(user: dict = Depends(current_user)):
    """Returns the caller's own subscription only."""
    subscription = get_subscription(user["uid"])
    if not subscription:
        return {"status": "not_started", "subscription": None}
    return {"status": subscription.get("status", "unknown"), "subscription": subscription}

@router.post("/cancel")
def request_cancellation(
    body: CancelSubscriptionRequest,
    user: dict = Depends(current_user),
):
    """Records a cancellation request. Provider cancellation API is a follow-up integration task."""
    ref = db.collection("subscriptions").document(user["uid"])
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="No subscription was found.")
    subscription = snap.to_dict() or {}
    if subscription.get("status") in {"cancelled", "expired"}:
        return {"status": subscription.get("status"), "message": "This subscription is already cancelled or expired."}

    ref.update({
        "cancellation_requested": True,
        "cancellation_requested_at": now_utc(),
        "cancellation_reason": body.reason,
        "updated_at": now_utc(),
    })
    # Do not falsely claim PayFast has been cancelled. A provider API call and confirmation are required.
    return {
        "status": "cancellation_requested",
        "message": "Your request has been recorded. Future billing is not confirmed cancelled yet; please wait for confirmation.",
    }
