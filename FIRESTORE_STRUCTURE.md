# SafeLink: how data is stored in Firestore

One person = one **root document**. Everything about that person sits under it.

## 1. The structure

```
users/
  jane-doe__DUCoM9ZE2iYWhhnEXRLGFO2gFeD3          <- ONE document per person ("name__uid")
  │   uid, user_ref, slug, display_name, email,
  │   created_at, updated_at, schema_version
  │   subscription: { status, plan_id, billing_mode, provider_token,
  │                   current_period_start/end, trial_ends_at, ... }     (a map)
  │   onboarding:   { step, saved[], skipped[], started, done }          (a map)
  │
  ├── profile/main            emergency profile: display_name, dob_hmac (hash), summary, notes, language
  ├── contacts/{contactId}    emergency contacts
  ├── vehicles/{vehicleId}    vehicles, with `images` (list of up to 5 photos)
  ├── tags/{qrId}             the person's claimed SafeLink tags (label, tag_ref, claimed_at)
  ├── payments/{paymentId}    every validated PayFast notification
  └── orders/{orderId}        prepaid (non-card) payment orders

qr_devices/{sha256(token)}            tag REGISTRY (see section 4)
emergency_access_tokens/{sha256(tok)} 10-minute token issued after a successful date-of-birth check
payment_webhook_events/{id}           log of public PayFast events with no user attached
```

Open `users/jane-doe__DUCoM9...` in the console and every contact, vehicle, tag,
payment and the subscription of that person is underneath it.

## 2. The document name: `name__uid`

| Part | What it is |
|------|------------|
| `jane-doe` | readable slug of the person's name (or email) **when the document was first created**. It is frozen and never changes. |
| `__` | separator |
| `DUCoM9ZE...` | the Firebase uid. Keeps the name unique. |

* The **current** name is always in the `display_name` field. It is updated when
  the person edits their profile. The document name stays as created.
* If the name is unknown, the email name is used, then the word `user`.
* The app cannot rebuild the name from the uid alone (the slug is frozen text),
  so it finds the document with a query `uid == ...` and **remembers the answer
  for as long as the server runs**. After the first request there is no extra
  cost. The code for this is `find_user_ref` / `ensure_user_ref` in
  `app/services/userstore.py`.
* Reads (`find_user_ref`) never create anything. Only writes (`ensure_user_ref`)
  create the document, the first time a person saves something.

## 3. Where each part of the code reads and writes

| Feature | Endpoint | Stored at |
|---------|----------|-----------|
| Emergency profile | `GET/PUT /profile/me` | `users/{id}/profile/main` |
| Contacts | `/profile/me/contacts` | `users/{id}/contacts/{contactId}` |
| Vehicles | `/profile/me/vehicles` | `users/{id}/vehicles/{vehicleId}` |
| Vehicle photos | `/profile/me/vehicles/{id}/images` | list inside the vehicle; files in Storage `vehicle_images/{uid}/...` |
| Tags (list/create/deactivate) | `/qr-tags` | `users/{id}/tags/{qrId}` + `qr_devices/{qrId}` |
| Claim a tag by scanning | `/qr-tags/claim/lookup`, `/confirm` | registry `qr_devices/{qrId}` + `users/{id}/tags/{qrId}` |
| Onboarding progress | `/onboarding/me` | `users/{id}` field `onboarding` |
| Subscription | PayFast + `/subscriptions/me` | `users/{id}` field `subscription` |
| Payments | `/payments/payfast/itn` | `users/{id}/payments/{paymentId}` |
| Prepaid orders | `/payments/payfast/checkout-prepaid` | `users/{id}/orders/{orderId}` |
| What a scanner sees | `/public/emergency` | read from the owner's profile, contacts, vehicles |

Only `app/services/userstore.py` contains these path names. Everything else asks
it for a reference (`contacts_col(ref)`, `vehicles_col(ref)`, `profile_doc(ref)`, ...).

Ownership: because a request can only reach the **signed-in person's own**
document, there is no `owner_uid` field to check in contacts/vehicles/tags. The
path is the ownership. Another person's id simply gives "not found".

## 4. Three collections stay at the top level, on purpose

* **`qr_devices/{sha256(token)}`**: a scan arrives knowing only the QR token,
  not the owner, so tags need a registry findable by the token's hash. It also
  holds tags nobody has claimed yet. It stores only routing and safety data:
  `tag_ref`, `sold`, `owner_uid`, `owner_ref` (the owner's document name),
  `active`, `failed_attempts`, `locked_until`, `last_verified_at`.
  `active` and `last_verified_at` live **only** here.
* **`emergency_access_tokens/{sha256(token)}`**: the 10-minute token a scanner
  receives after entering the correct date of birth. The scanner presents only
  this token, with no idea who owns it, so it must be findable by its hash. It
  holds `owner_uid`, `qr_id`, `expires_at`, `scope`. Expired tokens are deleted
  when presented, but one that is never presented would stay forever, so add a
  **TTL policy** on its `expires_at` field (Firestore console -> Time-to-live)
  to clean them up automatically.
* **`payment_webhook_events`**: public PayFast events that arrive with no user
  attached (and notifications for unknown people). It is a log.

## 5. Field notes

* `profile/main.dob_hmac` is a keyed hash of the date of birth. It is never sent
  to the browser (`clean_record` removes it).
* `subscription.status`: `checkout_pending`, `trialing`, `active`,
  `past_due_grace`, ...; access needs one of the last three AND a valid end date.
* `subscription.billing_mode`: `subscription` (card) or `prepaid`.
* PayFast `m_payment_id`: `<uid>` for card, `<uid>.<orderId>` for prepaid.
  The payment notification splits it on the first `.` to find whose document to update.
* A person's data is limited to 1 MiB for the root document. It only holds small
  maps; anything that grows (contacts, payments...) is in sub-collections.

## 6. Moving your existing data

Your old collections: `emergency_profiles`, `emergency_contacts`, `vehicles`,
`subscriptions`, `onboarding`, `payment_transactions`, `payment_orders`, `qr_devices`.

1. **Back up**: Firebase console -> Firestore -> Import/Export (or `gcloud firestore export`).
2. **Dry run** from your computer, with the new code and the API's environment
   variables (writes nothing; shows what it would do):
   `python -m scripts.migrate_to_user_documents`
3. **Copy**: `python -m scripts.migrate_to_user_documents --apply`
   (the old collections stay exactly as they were, so the live app is unaffected).
4. **Deploy the new backend code.**
5. **Run step 3 again** straight after deploying. It is safe to repeat, and it
   picks up anything saved between the first copy and the deploy.
6. **Check** with `python -m scripts.list_users` and by using the app.
7. **Clean up** once happy: `python -m scripts.migrate_to_user_documents --apply --delete-old`
   (never touches `qr_devices` or the webhook log).

It is safe to run more than once. To roll back before step 7: redeploy the old code;
the old collections were never modified. Document names use the profile's
`display_name`, else the name on the Firebase account, else the email name.

## 7. Finding people quickly

* `python -m scripts.list_users` prints every person: document, name, email,
  subscription, onboarded, contacts/vehicles/tags. `--find jane` filters.
  It warns if two documents ever exist for one uid.
* In the console: the list shows `name__uid`, so people are alphabetical by name.

## 8. Security and indexes

* Only the backend reads and writes Firestore (Admin SDK). Set the Firestore
  **rules to deny all client access**:
  `match /{document=**} { allow read, write: if false; }`
* No composite indexes are needed. The queries are single-field equality:
  `users where uid ==`, and `contacts where share_with_responder ==`.

## 9. Adding something new later

Put it under the person. Add a helper in `userstore.py`
(`def notes_col(ref): return ref.collection("notes")`), then use
`userstore.notes_col(userstore.ensure_user_ref(user))` in the endpoint. Small,
always-needed things can be a map on the root document (like `subscription`);
lists that grow belong in a sub-collection.

## 10. Checking that nothing still uses the old collections

Both `app/services/emergency.py` (profile lookups) and
`app/routers/subscriptions.py` (status and cancel) are already updated. To prove
no code opens an old top-level collection, run from the backend folder:

```
grep -rnE 'collection\("(emergency_profiles|emergency_contacts|vehicles|subscriptions|onboarding|payment_transactions|payment_orders)"\)' app/ scripts/
```

The only hit should be `ref.collection("vehicles")` in `userstore.py` (the new
sub-collection) and the migration script. Also note `GET /subscriptions/me` now
returns only safe fields (`status`, `has_access`, `subscription.plan_id`,
`subscription.current_period_end`). It no longer returns the PayFast token or the
consent record.
