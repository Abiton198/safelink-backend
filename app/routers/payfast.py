"""PayFast checkout and notification endpoints.

Important: configure PayFast's recurring-billing webhook URL to
/api/v1/payments/payfast/subscription-webhook in the merchant dashboard.
Never accept browser redirects as proof of payment.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from app.config import settings
from app.core.auth import current_user
from app.core.firebase import db
from app.core.payfast import generate_signature, validate_itn_with_payfast
from app.schemas.subscriptions import CheckoutRequest

router = APIRouter(prefix="/payments/payfast", tags=["PayFast"])

def utcnow():
    return datetime.now(timezone.utc)

def money(value: str) -> str:
    return f"{Decimal(value):.2f}"

@router.post("/checkout")
def create_checkout(body: CheckoutRequest, request: Request, user: dict = Depends(current_user)):
    """Creates signed PayFast form fields; the frontend posts these fields to PayFast."""
    if not body.accepted_recurring_terms:
        raise HTTPException(status_code=400, detail="You must agree to the recurring subscription terms.")
    if not settings.payfast_merchant_id or not settings.payfast_merchant_key or not settings.payfast_passphrase:
        raise HTTPException(status_code=503, detail="PayFast is not configured yet.")

    uid = user["uid"]
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Your account must have a verified email address.")

    # Idempotent local intent: this is not yet an active subscription.
    sub_ref = db.collection("subscriptions").document(uid)
    sub_ref.set({
        "user_id": uid,
        "plan_id": body.plan_id,
        "status": "checkout_pending",
        "provider": "payfast",
        "accepted_recurring_terms": True,
        "terms_version": body.terms_version,
        "consent_timestamp": utcnow(),
        "consent_ip": request.client.host if request.client else None,
        "trial_days": settings.trial_days,
        "amount": money(settings.subscription_monthly_amount),
        "currency": "ZAR",
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "cancellation_requested": False,
    }, merge=True)

    first_billing_date = (utcnow().date() + timedelta(days=settings.trial_days)).isoformat()
    fields = {
        "merchant_id": settings.payfast_merchant_id,
        "merchant_key": settings.payfast_merchant_key,
        "return_url": f"{settings.frontend_base_url}/subscription/return",
        "cancel_url": f"{settings.frontend_base_url}/subscription/cancelled",
        "notify_url": f"{settings.api_base_url}/api/v1/payments/payfast/itn",
        "name_first": (user.get("name") or "SafeLink User")[:100],
        "email_address": email[:100],
        "m_payment_id": uid,
        "amount": "0.00",
        "item_name": "SafeLink 7-day free trial",
        "item_description": "7-day trial, then monthly recurring subscription",
        "subscription_type": "1",
        "billing_date": first_billing_date,
        "recurring_amount": money(settings.subscription_monthly_amount),
        "frequency": "3",
        "cycles": "0",
        "subscription_notify_email": "true",
        "subscription_notify_webhook": "true",
        "subscription_notify_buyer": "true",
    }
    fields["signature"] = generate_signature(fields, settings.payfast_passphrase)
    return {
        "process_url": settings.payfast_process_url,
        "fields": fields,
        "trial_days": settings.trial_days,
        "first_billing_date": first_billing_date,
        "amount_after_trial": money(settings.subscription_monthly_amount),
        "currency": "ZAR",
        "message": "Post these fields to PayFast using an HTML form. Do not mark the subscription active from this response.",
    }

@router.post("/itn")
async def payfast_itn(request: Request):
    """Handles ordinary PayFast ITNs; payment is recorded only after remote validation."""
    form = await request.form()
    data = {str(k): str(v) for k, v in form.items()}
    if not validate_itn_with_payfast(data):
        raise HTTPException(status_code=400, detail="PayFast notification could not be validated.")

    if data.get("merchant_id") != settings.payfast_merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID mismatch.")

    uid = data.get("m_payment_id", "")
    if not uid:
        raise HTTPException(status_code=400, detail="Missing payment reference.")

    # Save a durable transaction record for audit/idempotency.
    payment_id = data.get("pf_payment_id") or data.get("token") or f"unknown-{int(utcnow().timestamp())}"
    db.collection("payment_transactions").document(payment_id).set({
        "provider": "payfast",
        "user_id": uid,
        "provider_payment_id": data.get("pf_payment_id"),
        "provider_token": data.get("token"),
        "payment_status": data.get("payment_status"),
        "amount_gross": data.get("amount_gross"),
        "received_at": utcnow(),
        "validated": True,
        "raw_payload": data,
    }, merge=True)

    # A zero-amount free-trial notification may not be represented as a normal COMPLETE ITN.
    # Do not activate the subscription here unless PayFast confirms an actual paid transaction.
    if data.get("payment_status") == "COMPLETE":
        ref = db.collection("subscriptions").document(uid)
        snap = ref.get()
        if snap.exists:
            existing = snap.to_dict() or {}
            ref.set({
                "status": "active",
                "provider_token": data.get("token") or existing.get("provider_token"),
                "last_payment_status": "COMPLETE",
                "last_payment_at": utcnow(),
                "current_period_start": utcnow(),
                "current_period_end": utcnow() + timedelta(days=30),
                "updated_at": utcnow(),
            }, merge=True)
    return JSONResponse({"received": True})

@router.post("/subscription-webhook")
async def subscription_webhook(request: Request):
    """Receives PayFast recurring-billing events (e.g. subscription.free-trial).

    Configure the webhook URL in PayFast. For production, verify the provider's
    webhook authenticity using the currently documented PayFast procedure before
    enabling activation based on this event.
    """
    payload = await request.json()
    event_type = payload.get("type")
    token = payload.get("token")
    if event_type not in {"subscription.free-trial", "subscription.promo", "subscription.update"} or not token:
        raise HTTPException(status_code=400, detail="Unsupported PayFast subscription event.")

    # IMPORTANT: this event endpoint intentionally does not activate a trial yet.
    # Before enabling activation, implement the currently documented PayFast webhook
    # authenticity check and securely correlate the event to a pending checkout.
    # An unauthenticated public webhook must never grant paid/trial access.
    db.collection("payment_webhook_events").add({
        "provider": "payfast",
        "event_type": event_type,
        "provider_token": token,
        "payload": payload,
        "received_at": utcnow(),
        "status": "pending_verification",
    })
    return {
        "received": True,
        "status": "pending_verification",
        "message": "Event logged only. Trial activation remains disabled until webhook verification and checkout correlation are implemented.",
    }
