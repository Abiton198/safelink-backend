"""Authenticated subscription status and cancellation-intent endpoints.

The subscription itself is the `subscription` map inside the person's own
document (users/{name}__{uid}); see services/subscriptions.py.
"""
from fastapi import APIRouter, Depends, HTTPException
from app.core.auth import current_user
from app.schemas.subscriptions import CancelSubscriptionRequest
from app.services.subscriptions import (
    get_subscription,
    get_subscription_summary,
    mark_cancellation_requested,
)

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

@router.get("/me")
def my_subscription(user: dict = Depends(current_user)):
    """The caller's own subscription status.

    A person who has not subscribed yet gets a normal answer
    (status "not_started"), not an error. Only safe fields are returned: the
    PayFast token and the consent record never leave the server.
    """
    return get_subscription_summary(user["uid"])

@router.post("/cancel")
def request_cancellation(
    body: CancelSubscriptionRequest,
    user: dict = Depends(current_user),
):
    """Records a cancellation request. Provider cancellation API is a follow-up integration task."""
    subscription = get_subscription(user["uid"])
    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription was found.")
    if subscription.get("status") in {"cancelled", "expired"}:
        return {"status": subscription.get("status"), "message": "This subscription is already cancelled or expired."}

    mark_cancellation_requested(user["uid"], body.reason)
    # Do not falsely claim PayFast has been cancelled. A provider API call and confirmation are required.
    return {
        "status": "cancellation_requested",
        "message": "Your request has been recorded. Future billing is not confirmed cancelled yet; please wait for confirmation.",
    }
