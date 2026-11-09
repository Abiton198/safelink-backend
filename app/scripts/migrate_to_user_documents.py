"""One-time migration: old separate collections -> one document per person.

BEFORE                                   AFTER
  emergency_profiles/{uid}                users/{name}__{uid}/profile/main
  emergency_contacts/{id}  (owner_uid)    users/{name}__{uid}/contacts/{id}
  vehicles/{id}            (owner_uid)    users/{name}__{uid}/vehicles/{id}
  subscriptions/{uid}                     users/{name}__{uid}  field `subscription`
  onboarding/{uid}                        users/{name}__{uid}  field `onboarding`
  qr_devices/{hash}  (owner_uid)          stays; gains `owner_ref`, plus
                                          users/{name}__{uid}/tags/{hash}
  payment_transactions/{id} (user_id)     users/{name}__{uid}/payments/{id}
  payment_orders/{id}       (user_id)     users/{name}__{uid}/orders/{id}

SAFE BY DEFAULT
  * Without --apply it only PRINTS what it would do (a dry run).
  * With --apply it COPIES. The old collections are left exactly as they were.
  * Running it again is harmless: documents are written under the same ids.
  * --delete-old (needs --apply) removes the old copies afterwards. Do that
    only after you have checked the new structure and the app works.
    qr_devices is never deleted: it stays as the tag registry.

HOW TO RUN (from the backend folder, with the API's environment variables)
    python -m scripts.migrate_to_user_documents                  # dry run
    python -m scripts.migrate_to_user_documents --apply
    python -m scripts.migrate_to_user_documents --apply --delete-old

Export a backup first (Firebase console -> Firestore -> Import/Export, or
`gcloud firestore export`).
"""
import argparse
from collections import defaultdict

from app.core.firebase import db
from app.services import userstore

# old collection -> (field holding the owner, new sub-collection)
MOVED = {
    "emergency_contacts": ("owner_uid", "contacts"),
    "vehicles": ("owner_uid", "vehicles"),
    "payment_transactions": ("user_id", "payments"),
    "payment_orders": ("user_id", "orders"),
}
# these old docs are keyed BY the uid: what they become -> the old collection
OLD_BY_UID = {
    "profile": "emergency_profiles",
    "subscription": "subscriptions",
    "onboarding": "onboarding",
}


def auth_details(uid: str) -> dict:
    """Name and email from Firebase Authentication ({} if the account is gone)."""
    try:
        from firebase_admin import auth

        account = auth.get_user(uid)
        return {"name": account.display_name or "", "email": account.email or ""}
    except Exception:  # noqa: BLE001 - deleted account, or auth not reachable
        return {}


def collect(owner_field_by_collection=MOVED):
    """Read the old collections. Returns {uid: {...everything belonging to uid}}."""
    people = defaultdict(lambda: {
        "profile": None, "subscription": None, "onboarding": None,
        "contacts": [], "vehicles": [], "payments": [], "orders": [], "tags": [],
    })

    for snap in db.collection("emergency_profiles").stream():
        people[snap.id]["profile"] = snap.to_dict() or {}
    for snap in db.collection("subscriptions").stream():
        people[snap.id]["subscription"] = snap.to_dict() or {}
    for snap in db.collection("onboarding").stream():
        people[snap.id]["onboarding"] = snap.to_dict() or {}

    for old, (owner_field, new) in owner_field_by_collection.items():
        for snap in db.collection(old).stream():
            data = snap.to_dict() or {}
            owner = data.get(owner_field)
            if owner:
                people[owner][new].append((snap.id, data, old))

    for snap in db.collection("qr_devices").stream():
        data = snap.to_dict() or {}
        if data.get("owner_uid"):
            people[data["owner_uid"]]["tags"].append((snap.id, data, "qr_devices"))

    return people


def migrate(apply: bool, delete_old: bool, details=auth_details, log=print) -> dict:
    """Run the migration. Returns counts (handy for checking and for tests)."""
    people = collect()
    totals = defaultdict(int)
    old_to_delete = []

    for uid, bundle in sorted(people.items()):
        profile = bundle["profile"] or {}
        account = details(uid)
        name = (profile.get("display_name") or account.get("name") or "").strip()
        email = account.get("email", "")

        existing = userstore.find_user_ref(uid)
        doc_id = existing.id if existing else userstore.make_user_doc_id(uid, name, email)

        log(f"\n{doc_id}")
        log(
            f"   profile={'yes' if bundle['profile'] else 'no'}  "
            f"subscription={(bundle['subscription'] or {}).get('status', 'none')}  "
            f"onboarding={'yes' if bundle['onboarding'] else 'no'}  "
            f"contacts={len(bundle['contacts'])}  vehicles={len(bundle['vehicles'])}  "
            f"tags={len(bundle['tags'])}  payments={len(bundle['payments'])}  "
            f"orders={len(bundle['orders'])}"
        )
        totals["people"] += 1

        if not apply:
            continue

        user_ref = existing or userstore.ensure_user_ref(
            {"uid": uid, "name": name, "email": email}
        )

        # Root-document maps
        root = {}
        if bundle["subscription"] is not None:
            root["subscription"] = bundle["subscription"]
        if bundle["onboarding"] is not None:
            root["onboarding"] = bundle["onboarding"]
        if name:
            root["display_name"] = name
        if root:
            userstore.touch_user(user_ref, **root)

        writes = []  # (reference, data)
        if bundle["profile"] is not None:
            writes.append((userstore.profile_doc(user_ref), bundle["profile"]))

        for new, factory in (
            ("contacts", userstore.contacts_col),
            ("vehicles", userstore.vehicles_col),
            ("payments", userstore.payments_col),
            ("orders", userstore.orders_col),
        ):
            for doc_id_old, data, old_name in bundle[new]:
                owner_key = MOVED[old_name][0]
                clean = {k: v for k, v in data.items() if k != owner_key}  # the path is the owner now
                writes.append((factory(user_ref).document(doc_id_old), clean))
                old_to_delete.append(db.collection(old_name).document(doc_id_old))

        for qr_id, data, _ in bundle["tags"]:
            writes.append((userstore.tags_col(user_ref).document(qr_id), {
                "qr_id": qr_id,
                "tag_ref": data.get("tag_ref", ""),
                "label": data.get("label") or "SafeLink tag",
                "claimed_at": data.get("claimed_at") or data.get("created_at"),
                "created_at": data.get("created_at"),
            }))
            # The registry stays; it just learns the owner's document name.
            db.collection("qr_devices").document(qr_id).set({"owner_ref": user_ref.id}, merge=True)

        for i in range(0, len(writes), 400):  # Firestore allows 500 writes per batch
            batch = db.batch()
            for ref, data in writes[i:i + 400]:
                batch.set(ref, data)
            batch.commit()

        for key, old_collection in OLD_BY_UID.items():
            if bundle[key] is not None:
                totals[key] += 1
                if delete_old:
                    old_to_delete.append(db.collection(old_collection).document(uid))
        for key in ("contacts", "vehicles", "tags", "payments", "orders"):
            totals[key] += len(bundle[key])

    if apply and delete_old:
        for ref in old_to_delete:
            ref.delete()
        totals["old_documents_deleted"] = len(old_to_delete)

    log("\n" + ("APPLIED." if apply else "DRY RUN: nothing was written. Add --apply to copy."))
    return dict(totals)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="really copy the data")
    parser.add_argument("--delete-old", action="store_true",
                        help="after copying, delete the old copies (needs --apply)")
    args = parser.parse_args()

    if args.delete_old and not args.apply:
        parser.error("--delete-old only works together with --apply")

    migrate(apply=args.apply, delete_old=args.delete_old)


if __name__ == "__main__":
    main()
