"""Firestore-backed subscription operations and access policy."""
from datetime import datetime, timezone
from fastapi import HTTPException
from app.core.firebase import db
from app.config import settings

def now_utc():
    return datetime.now(timezone.utc)

def get_subscription(uid: str) -> dict | None:
    doc = db.collection("subscriptions").document(uid).get()
    return doc.to_dict() if doc.exists else None

def require_active_subscription(uid: str) -> dict:
    subscription = get_subscription(uid)
    if not subscription:
        raise HTTPException(status_code=402, detail="An active SafeLink subscription is required.")
    status = subscription.get("status")
    if status not in {"trialing", "active", "past_due_grace"}:
        raise HTTPException(status_code=402, detail="Your SafeLink subscription is not active. Please renew to continue.")
    period_end = subscription.get("current_period_end")
    if period_end and hasattr(period_end, "timestamp") and period_end < now_utc():
        raise HTTPException(status_code=402, detail="Your SafeLink subscription has expired. Please renew to continue.")
    return subscription
