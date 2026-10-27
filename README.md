# SafeLink Backend — Stage 1: Subscription Foundation

This is a starter FastAPI backend for SafeLink's account subscription foundation:
Firebase Authentication, Firestore subscription records, PayFast recurring checkout
fields, ITN receipt, and a subscription status endpoint.

## Important status

This is **not production-ready yet**. Do not connect live PayFast credentials or use
this to gate real emergency information until the remaining security work in the
checklist below is complete and sandbox tests pass.

## Included

- FastAPI health endpoint.
- Firebase Admin token verification.
- Firestore subscription status endpoint.
- PayFast recurring subscription checkout fields:
  - R0.00 initial amount for a free trial.
  - Seven-day trial.
  - R19.99 monthly recurring amount (configurable).
  - Indefinite monthly cycles.
  - PayFast buyer, merchant and webhook reminder flags.
- PayFast ITN remote validation and transaction audit records.
- Trial subscription webhook endpoint.
- Cancellation request recording (does not yet cancel at PayFast).

## 1. Create a dedicated Firebase project

Create a separate Firebase project for SafeLink. Enable Authentication (Email/Password
or your chosen sign-in methods) and create a Firestore database. Create a service-account
credential for local development and save it as:

`secrets/firebase-service-account.json`

Never commit this file or your `.env` file.

## 2. Configure the backend

```bash
cp .env .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `.env` with your Firebase project ID, PayFast sandbox credentials, passphrase,
frontend URL, and a publicly reachable API URL. The ITN and subscription webhook
URLs must be reachable by PayFast; `localhost` will not work for callbacks.

## 3. Run locally

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Visit `http://localhost:8000/docs` and `http://localhost:8000/api/v1/health`.

## 4. PayFast dashboard setup

1. Use a PayFast Sandbox account first.
2. Enable recurring billing and configure the required security passphrase.
3. Set the recurring-billing subscription webhook URL to:
   `https://YOUR-PUBLIC-API-HOST/api/v1/payments/payfast/subscription-webhook`
4. Ensure your normal ITN endpoint is:
   `https://YOUR-PUBLIC-API-HOST/api/v1/payments/payfast/itn`
5. Test free-trial creation, initial R0.00 card setup, first recurring charge, cancellation,
   duplicate notifications, and failed payments before live launch.

PayFast documents a minimum future recurring charge of R5.00. The seven-day trial's
initial amount can be R0.00, while the recurring amount must be at least R5.00.

## 5. Frontend checkout contract

The signed-in frontend calls:

`POST /api/v1/payments/payfast/checkout`

with a Firebase bearer token and JSON such as:

```json
{
  "plan_id": "safelink_monthly",
  "accepted_recurring_terms": true,
  "terms_version": "safelink-subscription-v1"
}
```

The backend returns `process_url` and `fields`. The frontend must submit those fields
as an HTML POST form to `process_url`. Never treat the return/cancel browser URL as
payment confirmation. Only trusted, validated provider notifications can change
subscription state.

## 6. Before production — required hardening

- Verify PayFast's current ITN validation steps, including signature, merchant identity,
  amount, source IP where applicable, and server validation. Ensure recurring trial
  webhook authenticity using the currently documented PayFast procedure.
- Do not activate a trial from an unverified webhook. Link provider tokens to a known
  pending checkout securely and handle webhook/ITN arrival order.
- Implement PayFast API cancellation and confirm cancellation with PayFast before telling
  the user it is complete.
- Implement idempotent event processing and proper status transitions for every recurring
  payment, failure, refund, chargeback, pause, cancellation and reactivation.
- Calculate period end dates from the actual plan and verified provider billing schedule.
- Implement trial reminder delivery and store delivery results. PayFast's own trial reminder
  configuration must also be checked in the merchant dashboard.
- Add rate limiting, structured logs, monitoring, tests and secret rotation.
- Enforce `require_active_subscription()` on every protected profile, vehicle, device and
  emergency-data route. Do not rely on frontend UI gating.
- Confirm legal terms, consent wording, privacy notice and cancellation rules before launch.
- Add automated tests with mocked Firebase/PayFast responses and complete sandbox testing.

## Current API routes

- `GET /api/v1/health`
- `GET /api/v1/subscriptions/me`
- `POST /api/v1/subscriptions/cancel` (records request only; does not yet cancel provider billing)
- `POST /api/v1/payments/payfast/checkout`
- `POST /api/v1/payments/payfast/itn`
- `POST /api/v1/payments/payfast/subscription-webhook`

## Subscription data

The backend stores subscription records in Firestore `subscriptions`, payment events in
`payment_transactions`, and recurring webhook records in `payment_webhook_events`.
No full card number or CVV is stored.
