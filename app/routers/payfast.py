"""PayFast checkout and notification endpoints.

WHERE THE DATA LIVES  (see services/userstore.py for the whole structure)
-------------------------------------------------------------------------
  users/{name}__{uid}
      subscription: { ... }          <- plan, status, PayFast token, dates
      payments/{paymentId}           <- every validated PayFast notification
      orders/{orderId}               <- prepaid (non-card) payment orders
  payment_webhook_events             <- the one top-level log: public events
                                        that arrive with no user attached

HOW PAYFAST FINDS THE RIGHT PERSON  (m_payment_id)
--------------------------------------------------
  card + free trial   m_payment_id = "<uid>"
  prepaid payment     m_payment_id = "<uid>.<orderId>"
The notification handler splits that on the first "." to know whose document
to update, without searching anything.

HOW THE FREE TRIAL STARTS (and why onboarding needs a card)
-----------------------------------------------------------
1. The owner finishes onboarding and presses "Add card & start free trial".
2. POST /checkout builds a PayFast subscription with an INITIAL amount of
   R0.00 and a first billing date after the trial. PayFast shows its own page
   where the customer enters card details and completes 3D Secure. No money is
   taken. SafeLink never sees the card number.
3. PayFast then sends an ITN (server-to-server notification) with
   payment_status=COMPLETE, amount_gross=0.00 and a subscription `token`.
   THIS starts the trial: the subscription changes from "checkout_pending" to
   "trialing" (see payfast_itn below).
4. When the trial ends, PayFast charges the first monthly amount and sends
   another ITN with the real amount; the subscription becomes "active".

PAYING WITHOUT A CARD (prepaid access)
--------------------------------------
PayFast recurring billing works on a saved CARD token, so automatic monthly
billing is credit/cheque-card only. Anyone who cannot or will not use a card
can PREPAY for 1, 3 or 12 months with a one-off payment, using any method
PayFast offers them. That path (/checkout-prepaid) has no free trial and does
not renew automatically; it is tracked by an order under the person's document
so an active subscription is never overwritten while a payment is pending, and
access is granted only when PayFast's validated ITN shows the exact amount.

Why not the webhook? PayFast's `subscription.free-trial` webhook is a reminder
that a trial is about to end, not proof a card was just added, so it is only
logged (see subscription_webhook).

Never accept browser redirects as proof of payment: the return page only shows
what the ITN has already recorded.

Configure the recurring-billing webhook URL in the PayFast dashboard to
/api/v1/payments/payfast/subscription-webhook.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore
from pydantic import BaseModel

from app.config import settings
from app.core.auth import current_user
from app.core.firebase import db
from app.core.payfast import generate_signature, passphrase_problems, validate_itn_with_payfast
from app.schemas.subscriptions import CheckoutRequest
from app.services import userstore
from app.services.subscriptions import (
    access_problem,
    get_subscription,
    normalise_datetime,
    update_subscription,
)

logger = logging.getLogger("uvicorn.error")

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


def _require_payfast_configured() -> None:
    missing = [
        name
        for name, value in (
            ("PAYFAST_MERCHANT_ID", settings.payfast_merchant_id),
            ("PAYFAST_MERCHANT_KEY", settings.payfast_merchant_key),
            ("PAYFAST_PASSPHRASE", settings.payfast_passphrase),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail="PayFast is not configured. Missing: " + ", ".join(missing),
        )


def _log_checkout(kind: str) -> None:
    """Facts only, never secrets: helps spot sandbox/live and passphrase mix-ups."""
    logger.info(
        "PayFast %s: sandbox=%s process_url=%s merchant_id=%s passphrase_len=%d problems=%s",
        kind,
        settings.payfast_sandbox,
        settings.payfast_process_url,
        settings.payfast_merchant_id,
        len(settings.payfast_passphrase),
        passphrase_problems(settings.payfast_passphrase) or "none",
    )


# ---------------------------------------------------------------------------
# Card + free trial
# ---------------------------------------------------------------------------
@router.post("/checkout")
def create_checkout(body: CheckoutRequest, request: Request, user: dict = Depends(current_user)):
    """Creates signed PayFast form fields; the frontend posts these fields to PayFast.

    No subscription is needed to reach this endpoint: it is the LAST step of
    onboarding, after the owner has saved or skipped the earlier steps.
    """
    if not body.accepted_recurring_terms:
        raise HTTPException(status_code=400, detail="You must agree to the recurring subscription terms.")
    _require_payfast_configured()

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
    user_ref = userstore.ensure_user_ref(user)
    update_subscription(user_ref, {
        "user_id": uid,
        "plan_id": body.plan_id,
        "status": "checkout_pending",
        "provider": "payfast",
        "billing_mode": "subscription",
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
    })

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
    _log_checkout("checkout")
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


# ---------------------------------------------------------------------------
# Prepaid (no card)
# ---------------------------------------------------------------------------
PrepaidMonths = Literal[1, 3, 12]


class PrepaidCheckoutRequest(BaseModel):
    months: PrepaidMonths
    accepted_terms: bool
    terms_version: str = "safelink-prepaid-v1"


@router.post("/checkout-prepaid")
def create_prepaid_checkout(
    body: PrepaidCheckoutRequest,
    request: Request,
    user: dict = Depends(current_user),
):
    """One-off PayFast payment for 1, 3 or 12 months of access (no card needed).

    The customer picks any payment method PayFast offers on its page. No free
    trial and no automatic renewal on this path.

    The subscription is NOT touched here. A pending ORDER is created under the
    person's document, so someone who already has access cannot lose it by
    starting (or abandoning) a payment. Access is granted only by the ITN.
    """
    if not body.accepted_terms:
        raise HTTPException(status_code=400, detail="You must accept the payment terms.")
    _require_payfast_configured()

    uid = user["uid"]
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Your account must have a verified email address.")

    # Someone on an automatic card subscription (or trial) already has access.
    # A prepaid customer MAY buy more time before theirs runs out.
    existing = get_subscription(uid)
    if (
        existing
        and access_problem(existing) is None
        and existing.get("billing_mode") != "prepaid"
    ):
        raise HTTPException(
            status_code=409,
            detail="You already have an active SafeLink subscription.",
        )

    amount = Decimal(settings.subscription_monthly_amount) * body.months
    order_id = uuid4().hex
    user_ref = userstore.ensure_user_ref(user)

    userstore.orders_col(user_ref).document(order_id).set({
        "kind": "prepaid",
        "months": body.months,
        "amount": f"{amount:.2f}",
        "currency": "ZAR",
        "status": "pending",
        "terms_version": body.terms_version,
        "consent_timestamp": utcnow(),
        "consent_ip": request.client.host if request.client else None,
        "created_at": utcnow(),
    })

    fields = {
        "merchant_id": settings.payfast_merchant_id,
        "merchant_key": settings.payfast_merchant_key,
        "return_url": f"{settings.frontend_base_url}/subscription/return",
        "cancel_url": f"{settings.frontend_base_url}/subscription/cancelled",
        "notify_url": f"{settings.api_base_url}/api/v1/payments/payfast/itn",
        "name_first": (user.get("name") or "SafeLink User")[:100],
        "email_address": email[:100],
        "m_payment_id": f"{uid}.{order_id}",  # tells the ITN whose order this is
        "amount": f"{amount:.2f}",
        "item_name": f"SafeLink {body.months}-month access",
        "item_description": "Prepaid SafeLink emergency access. Does not renew automatically.",
    }
    _log_checkout("checkout-prepaid")
    fields["signature"] = generate_signature(fields, settings.payfast_passphrase)
    return {
        "process_url": settings.payfast_process_url,
        "fields": fields,
        "months": body.months,
        "amount": f"{amount:.2f}",
        "currency": "ZAR",
        "message": "Post these fields to PayFast using an HTML form. Access starts only after PayFast confirms payment.",
    }


@firestore.transactional
def settle_prepaid_order(transaction, order_ref, user_ref, pf_payment_id, gross) -> bool:
    """Marks a prepaid order paid and extends access, exactly once.

    Runs inside a Firestore transaction, so two copies of the same ITN
    arriving together cannot both extend the user's access.
    Returns True only if access was granted by THIS call.
    """
    order_snap = order_ref.get(transaction=transaction)
    if not order_snap.exists:
        return False

    order = order_snap.to_dict() or {}
    if order.get("status") != "pending":
        return False  # already settled: duplicate notification

    if gross != Decimal(order.get("amount", "-1")):
        return False  # amount does not match what we asked for

    now = utcnow()
    months = int(order.get("months", 1))

    # Buying more time while still prepaid-active adds on to the current end.
    start_from = now
    user_snap = user_ref.get(transaction=transaction)
    current = ((user_snap.to_dict() or {}).get("subscription") or {}) if user_snap.exists else {}
    if current.get("billing_mode") == "prepaid":
        current_end = normalise_datetime(current.get("current_period_end"))
        if current_end and current_end > now:
            start_from = current_end

    transaction.update(order_ref, {
        "status": "paid",
        "pf_payment_id": pf_payment_id,
        "paid_at": now,
    })
    transaction.set(user_ref, {
        "subscription": {
            "provider": "payfast",
            "billing_mode": "prepaid",
            "status": "active",
            "last_payment_status": "COMPLETE",
            "last_payment_at": now,
            "current_period_start": now,
            "current_period_end": start_from + timedelta(days=30 * months),
            "updated_at": now,
        },
        "updated_at": now,
    }, merge=True)
    return True


# ---------------------------------------------------------------------------
# PayFast notifications
# ---------------------------------------------------------------------------
@router.post("/itn")
async def payfast_itn(request: Request):
    """Handles PayFast ITNs. Access changes only AFTER remote validation.

    Payments that can grant access:
      * card, amount_gross 0.00      -> card saved: start the FREE TRIAL
      * card, amount_gross = monthly -> real payment: subscription ACTIVE
      * prepaid order, exact amount  -> access extended (see settle_prepaid_order)
    Anything else is recorded for audit but never grants access.
    """
    form = await request.form()
    data = {str(k): str(v) for k, v in form.items()}
    if not validate_itn_with_payfast(data):
        raise HTTPException(status_code=400, detail="PayFast notification could not be validated.")

    if data.get("merchant_id") != settings.payfast_merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID mismatch.")

    reference = data.get("m_payment_id", "")
    if not reference or "/" in reference or len(reference) > 200:
        raise HTTPException(status_code=400, detail="Missing or invalid payment reference.")

    # "<uid>" = card subscription, "<uid>.<orderId>" = prepaid order.
    uid, _, order_id = reference.partition(".")

    user_ref = userstore.find_user_ref(uid)
    if user_ref is None:
        # Valid PayFast notification but we have no such person: keep a record.
        logger.error("PayFast ITN for unknown user reference %s", reference)
        db.collection("payment_webhook_events").add({
            "provider": "payfast",
            "event_type": "itn_unknown_user",
            "reference": reference,
            "payload": data,
            "received_at": utcnow(),
            "status": "unmatched",
        })
        return JSONResponse({"received": True})

    # Save a durable record under the person, for audit and idempotency.
    payment_id = data.get("pf_payment_id") or data.get("token") or f"unknown-{int(utcnow().timestamp())}"
    userstore.payments_col(user_ref).document(payment_id).set({
        "provider": "payfast",
        "order_id": order_id or None,
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

    gross = to_decimal(data.get("amount_gross"))

    # ---- Prepaid (non-card) payment ----
    if order_id:
        if gross is not None:
            settle_prepaid_order(
                db.transaction(),
                userstore.orders_col(user_ref).document(order_id),
                user_ref,
                data.get("pf_payment_id"),
                gross,
            )
        return JSONResponse({"received": True})

    # ---- Card subscription ----
    existing = get_subscription(uid)
    if not existing:
        # No checkout from us for this user: record only, grant nothing.
        return JSONResponse({"received": True})

    token = data.get("token")
    stored_token = existing.get("provider_token")

    # A notification for a DIFFERENT PayFast subscription than the one we are
    # tracking for this user must never change their access.
    if token and stored_token and token != stored_token:
        return JSONResponse({"received": True})

    if gross is not None and gross == 0:
        # ---- Card saved, nothing charged: START THE TRIAL ----
        # Only from a pending checkout. A repeated/duplicate ITN for a user who
        # is already trialing or active is ignored, so it cannot reset or
        # extend their trial.
        if existing.get("status") == "checkout_pending" and token:
            trial_end = (
                normalise_datetime(existing.get("trial_ends_at"))
                or utcnow() + timedelta(days=settings.trial_days)
            )
            update_subscription(user_ref, {
                "status": "trialing",
                "provider_token": token,
                "trial_started_at": utcnow(),
                "last_payment_status": "COMPLETE",
                "current_period_start": utcnow(),
                "current_period_end": trial_end,
                "updated_at": utcnow(),
            })

    elif gross is not None and gross == Decimal(money(settings.subscription_monthly_amount)):
        # ---- Real monthly payment (first charge after trial, or a renewal) ----
        # An already-active subscriber legitimately receives one of these every
        # month, so this must not reject active users.
        update_subscription(user_ref, {
            "status": "active",
            "provider_token": token or stored_token,
            "last_payment_status": "COMPLETE",
            "last_payment_at": utcnow(),
            "current_period_start": utcnow(),
            "current_period_end": utcnow() + timedelta(days=30),
            "updated_at": utcnow(),
        })

    # Any other amount: recorded above, but never activates access.
    return JSONResponse({"received": True})


@router.post("/subscription-webhook")
async def subscription_webhook(request: Request):
    """Receives PayFast recurring-billing events (e.g. subscription.free-trial).

    PayFast sends these shortly BEFORE a trial ends (or before an amount
    changes). They are reminders, not proof a card was just added, so they are
    only logged. The trial itself is started by the ITN above.

    This URL is public. Never grant access from it without verifying the event
    with PayFast's currently documented procedure.
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
