"""Create a batch of UNCLAIMED SafeLink tags and a CSV for the QR printer.

Run from the backend folder (with the same environment variables as the API):

    python -m scripts.generate_tags --count 50 --sold --out batch1.csv

Each tag becomes a record in `qr_devices` with no owner. The CSV lists every
tag's reference and its scan address (https://<site>/scan/<token>): print each
address as a QR code on its tag.

IMPORTANT: the CSV contains the secret token of every tag. Only the hash is
stored in the database, so the CSV is the ONLY copy. Keep it private, give it
only to whoever prints the tags, and delete it afterwards.

  --sold   mark the tags as sold now (needed when TAG_CLAIM_REQUIRES_SOLD=true).
           Without it they are created as not sold; mark them sold later.
"""
import argparse
import csv
import hashlib
import secrets

from google.cloud import firestore

from app.config import settings
from app.core.firebase import db
from app.services.emergency import now_utc


def next_number(prefix: str) -> int:
    """One more than the highest existing number for this prefix."""
    last = list(
        db.collection("qr_devices")
        .where("tag_ref", ">=", f"{prefix}-")
        .where("tag_ref", "<", f"{prefix}.")  # "." sorts just after "-"
        .order_by("tag_ref", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    if not last:
        return 1
    return int(last[0].to_dict()["tag_ref"].rsplit("-", 1)[-1]) + 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--count", type=int, required=True, help="how many tags")
    parser.add_argument("--prefix", default="SL", help="reference prefix (default SL)")
    parser.add_argument("--sold", action="store_true", help="mark the tags as sold")
    parser.add_argument("--out", default="tags.csv", help="CSV file to write")
    args = parser.parse_args()

    if not 1 <= args.count <= 2000:
        parser.error("--count must be between 1 and 2000")

    start = next_number(args.prefix)
    base = settings.frontend_base_url.rstrip("/")
    now = now_utc()

    rows = []
    batch = db.batch()
    pending = 0

    for offset in range(args.count):
        token = secrets.token_urlsafe(32)
        qr_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        tag_ref = f"{args.prefix}-{start + offset:06d}"

        batch.set(
            db.collection("qr_devices").document(qr_id),
            {
                "tag_ref": tag_ref,
                "label": "SafeLink tag",
                "owner_uid": None,   # unclaimed
                "active": False,     # switched on when claimed
                "sold": bool(args.sold),
                "failed_attempts": 0,
                "locked_until": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        rows.append((tag_ref, f"{base}/scan/{token}"))
        pending += 1

        if pending == 400:  # Firestore allows 500 writes per batch
            batch.commit()
            batch, pending = db.batch(), 0

    if pending:
        batch.commit()

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tag_ref", "scan_url"])
        writer.writerows(rows)

    print(f"Created {args.count} tags ({rows[0][0]} to {rows[-1][0]}), "
          f"sold={bool(args.sold)}.")
    print(f"Wrote {args.out}. It holds the secret tag addresses: keep it private.")


if __name__ == "__main__":
    main()