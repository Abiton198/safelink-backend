"""PayFast signature helpers and server-side ITN verification."""
import hashlib
import re
from urllib.parse import quote_plus
import requests
from fastapi import HTTPException
from app.config import settings

# PayFast's support article says a passphrase may contain only letters,
# numbers and - _ /  (anything else is a common cause of signature errors).
_PASSPHRASE_SAFE = re.compile(r"^[A-Za-z0-9_/\-]+$")


def php_urlencode(value) -> str:
    """URL-encode exactly like PHP's urlencode(trim($value)), which PayFast uses.

    Everything except letters, digits and - _ . is percent-encoded (upper-case
    hex), and spaces become "+". Python never encodes "~" by itself, but PHP
    does (%7E), so it is replaced here.
    """
    return quote_plus(str(value).strip(), safe="").replace("~", "%7E")


def generate_signature(data: dict, passphrase: str = "") -> str:
    """MD5 signature for a PayFast checkout form.

    Rules (from PayFast):
      * fields are used in the SAME ORDER they are posted (dict order here),
      * blank fields are left out, "signature" itself is left out,
      * values are trimmed and URL-encoded,
      * the passphrase, if any, is appended last as &passphrase=...
    """
    pairs = []
    for key, value in data.items():
        if key == "signature" or value is None or str(value).strip() == "":
            continue
        pairs.append(f"{key}={php_urlencode(value)}")
    query = "&".join(pairs)
    if passphrase and passphrase.strip():
        query += "&passphrase=" + php_urlencode(passphrase)
    return hashlib.md5(query.encode("utf-8")).hexdigest()


def passphrase_problems(passphrase: str) -> list[str]:
    """Plain-language warnings about the passphrase. Never includes its value."""
    problems = []
    if not passphrase:
        problems.append("it is empty")
        return problems
    if passphrase != passphrase.strip():
        problems.append("it has a space or line break at the start or end")
    if not _PASSPHRASE_SAFE.match(passphrase.strip()):
        problems.append("it contains characters other than letters, numbers and - _ /")
    return problems


def validate_itn_with_payfast(form_data: dict) -> bool:
    """Post ITN payload to PayFast's validation endpoint; fail closed on errors."""
    if not settings.payfast_merchant_id:
        return False
    try:
        response = requests.post(
            settings.payfast_itn_validate_url,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return response.status_code == 200 and response.text.strip() == "VALID"
    except requests.RequestException:
        return False