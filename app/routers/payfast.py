"""PayFast checkout and notification endpoints.

HOW THE FREE TRIAL STARTS (and why onboarding needs a card)
-----------------------------------------------------------
1. The owner finishes onboarding and presses "Add card & start free trial".
2. POST /checkout builds a PayFast subscription with an INITIAL amount of
   R0.00 and a first billing date after the trial. PayFast shows its own
   page where the customer enters card details and completes 3D Secure.
   No money is taken. SafeLink never sees the card number.
3. PayFast then sends an ITN (server-to-server notification) with
   payment_status=COMPLETE, amount_gross=0.00 and a subscription `token`.
   THIS is what starts the trial: the subscription changes from
   "checkout_pending" to "trialing" (see payfast_itn below).
4. When the trial ends, PayFast charges the first monthly amount and sends
   another ITN with the real amount; the subscription becomes "active".

Why not the webhook? PayFast's `subscription.free-trial` webhook is a
reminder that a trial is about to end. It does not mean the card was just
added, so it is only logged here (see subscription_webhook).

Never accept browser redirects as proof of payment: the return page only
shows what the ITN has already recorded.

Configure the recurring-billing webhook URL in the PayFast dashboard to
/api/v1/payments/payfast/subscription-webhook.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from app.config import settings
from app.core.auth import current_user
from app.core.firebase import db
from app.core.payfast import generate_signature, validate_itn_with_payfast
from app.schemas.subscriptions import CheckoutRequest
from app.services.subscriptions import (
    access_problem,
    get_subscription,
    normalise_datetime,
)

router = APIRouter(prefix="/payments/payfast", tags=["PayFast"])

def utcnow():
    return datetime.now(timezone.utc)

def money(value: str) -> str:
    return f"{Decimal(value):.2f}"

def to_decimal(value) -> Decimal | None:
    """Parse an amount from a notification; None if it is not a number."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

@router.post("/checkout")
def create_checkout(body: CheckoutRequest, request: Request, user: dict = Depends(current_user)):
    """Creates signed PayFast form fields; the frontend posts these fields to PayFast.

    No subscription is needed to reach this endpoint: it is the LAST step of
    onboarding, after the owner has saved or skipped the earlier steps.
    """
    if not body.accepted_recurring_terms:
        raise HTTPException(status_code=400, detail="You must agree to the recurring subscription terms.")
    if not settings.payfast_merchant_id or not settings.payfast_merchant_key or not settings.payfast_passphrase:
        raise HTTPException(status_code=503, detail="PayFast is not configured yet.")

    uid = user["uid"]
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Your account must have a verified email address.")

    # Do not start a second checkout for someone who already has access
    # (trialing or active). Without this, a double tap or a second tab would
    # overwrite a working subscription with "checkout_pending".
    existing_subscription = get_subscription(uid)
    if existing_subscription and access_problem(existing_subscription) is None:
        raise HTTPException(
            status_code=409,
            detail="You already have an active SafeLink subscription.",
        )

    # When PayFast first bills (after the trial), and when trial access ends:
    # the end of that billing day, so access never lapses before the charge.
    first_billing = utcnow().date() + timedelta(days=settings.trial_days)
    first_billing_date = first_billing.isoformat()
    trial_ends_at = datetime(
        first_billing.year, first_billing.month, first_billing.day,
        tzinfo=timezone.utc,
    ) + timedelta(days=1)

    # Local intent only: this is NOT an active subscription yet. It becomes
    # "trialing" when PayFast's ITN confirms the card was saved.
    sub_ref = db.collection("subscriptions").document(uid)
    sub_ref.set({
        "user_id": uid,
        "plan_id": body.plan_id,
        "status": "checkout_pending",
        "provider": "payfast",
        "provider_token": None,  # a fresh checkout gets a fresh PayFast token
        "accepted_recurring_terms": True,
        "terms_version": body.terms_version,
        "consent_timestamp": utcnow(),
        "consent_ip": request.client.host if request.client else None,
        "trial_days": settings.trial_days,
        "first_billing_date": first_billing_date,
        "trial_ends_at": trial_ends_at,
        "amount": money(settings.subscription_monthly_amount),
        "currency": "ZAR",
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "cancellation_requested": False,
    }, merge=True)

    fields = {
        "merchant_id": settings.payfast_merchant_id,
        "merchant_key": settings.payfast_merchant_key,
        "return_url": f"{settings.frontend_base_url}/subscription/return",
        "cancel_url": f"{settings.frontend_base_url}/subscription/cancelled",
        "notify_url": f"{settings.api_base_url}/api/v1/payments/payfast/itn",
        "name_first": (user.get("name") or "SafeLink User")[:100],
        "email_address": email[:100],
        "m_payment_id": uid,
        "amount": "0.00",  # R0.00 today: card details are saved, nothing is charged
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
    """Handles PayFast ITNs. Access changes only AFTER remote validation.

    Two payments matter:
      * amount_gross 0.00      -> card saved: start the FREE TRIAL
      * amount_gross = monthly -> real payment: subscription ACTIVE
    Anything else is recorded for audit but never grants access.
    """
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

    if data.get("payment_status") != "COMPLETE":
        return JSONResponse({"received": True})

    ref = db.collection("subscriptions").document(uid)
    snap = ref.get()
    if not snap.exists:
        # No checkout from us for this user: record only, grant nothing.
        return JSONResponse({"received": True})

    existing = snap.to_dict() or {}
    token = data.get("token")
    stored_token = existing.get("provider_token")
    gross = to_decimal(data.get("amount_gross"))

    # A notification for a DIFFERENT PayFast subscription than the one we are
    # tracking for this user must never change their access.
    if token and stored_token and token != stored_token:
        return JSONResponse({"received": True})

    if gross is not None and gross == 0:
        # ---- Card saved, nothing charged: START THE TRIAL ----
        # Only from a pending checkout. A repeated/duplicate ITN for a user
        # who is already trialing or active is ignored, so it cannot reset
        # or extend their trial.
        if existing.get("status") == "checkout_pending" and token:
            trial_end = (
                normalise_datetime(existing.get("trial_ends_at"))
                or utcnow() + timedelta(days=settings.trial_days)
            )
            ref.set({
                "status": "trialing",
                "provider_token": token,
                "trial_started_at": utcnow(),
                "last_payment_status": "COMPLETE",
                "current_period_start": utcnow(),
                "current_period_end": trial_end,
                "updated_at": utcnow(),
            }, merge=True)

    elif gross is not None and gross == Decimal(money(settings.subscription_monthly_amount)):
        # ---- Real monthly payment (first charge after trial, or a renewal) ----
        # An already-active subscriber legitimately receives one of these
        # every month, so this must not reject active users.
        ref.set({
            "status": "active",
            "provider_token": token or stored_token,
            "last_payment_status": "COMPLETE",
            "last_payment_at": utcnow(),
            "current_period_start": utcnow(),
            "current_period_end": utcnow() + timedelta(days=30),
            "updated_at": utcnow(),
        }, merge=True)

    # Any other amount: recorded above, but never activates access.
    return JSONResponse({"received": True})

@router.post("/subscription-webhook")
async def subscription_webhook(request: Request):
    """Receives PayFast recurring-billing events (e.g. subscription.free-trial).

    PayFast sends these shortly BEFORE a trial ends (or before an amount
    changes). They are reminders, not proof a card was just added, so they are
    only logged. The trial itself is started by the ITN above.

    This URL is public. Never grant access from it without verifying the
    event with PayFast's currently documented procedure.
    """
    payload = await request.json()
    event_type = payload.get("type")
    token = payload.get("token")
    if event_type not in {"subscription.free-trial", "subscription.promo", "subscription.update"} or not token:
        raise HTTPException(status_code=400, detail="Unsupported PayFast subscription event.")

    db.collection("payment_webhook_events").add({
        "provider": "payfast",
        "event_type": event_type,
        "provider_token": token,
        "payload": payload,
        "received_at": utcnow(),
        "status": "logged_only",
    })
    return {"received": True, "status": "logged_only"}