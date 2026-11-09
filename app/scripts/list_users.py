"""List everyone in Firestore in one readable table (easy tracking).

    python -m scripts.list_users
    python -m scripts.list_users --find jane        # name, email or uid contains "jane"

Shows each person's document name, name, email, subscription status and how
much data they have, so you never have to click through the console. It also
warns about the one thing that should never happen: two documents for the same uid.
"""
import argparse
from collections import Counter

from app.core.firebase import db
from app.services import userstore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--find", default="", help="only people matching this text")
    args = parser.parse_args()
    needle = args.find.lower()

    rows, seen = [], Counter()
    for snap in db.collection(userstore.USERS).stream():
        data = snap.to_dict() or {}
        seen[data.get("uid")] += 1
        text = f"{snap.id} {data.get('display_name', '')} {data.get('email', '')}".lower()
        if needle and needle not in text:
            continue

        count = lambda col: sum(1 for _ in snap.reference.collection(col).stream())  # noqa: E731
        rows.append((
            snap.id,
            data.get("display_name", ""),
            data.get("email", ""),
            (data.get("subscription") or {}).get("status", "-"),
            "yes" if (data.get("onboarding") or {}).get("done") else "no",
            count("contacts"), count("vehicles"), count("tags"),
        ))

    header = ("DOCUMENT", "NAME", "EMAIL", "SUBSCRIPTION", "ONBOARDED", "CONTACTS", "VEHICLES", "TAGS")
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    for row in [header] + rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))

    print(f"\n{len(rows)} people shown.")
    duplicates = [uid for uid, n in seen.items() if n > 1]
    if duplicates:
        print("WARNING: more than one document for uid(s):", ", ".join(duplicates))


if __name__ == "__main__":
    main()
