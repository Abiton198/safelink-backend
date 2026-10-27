"""PayFast signature helpers and server-side ITN verification."""
import hashlib
from urllib.parse import quote_plus
import requests
from fastapi import HTTPException
from app.config import settings

def _payfast_value(value) -> str:
    # PayFast signatures use URL-encoded field values and a passphrase for recurring billing.
    return str(value).strip()

def generate_signature(data: dict, passphrase: str = "") -> str:
    # Exclude signature; retain insertion order as posted by our generated checkout fields.
    pairs = []
    for key, value in data.items():
        if key == "signature" or value is None or value == "":
            continue
        encoded_value = quote_plus(_payfast_value(value), safe="-_.~")
        pairs.append(f"{key}={encoded_value}")
    query = "&".join(pairs)
    if passphrase:
        query += "&passphrase=" + quote_plus(_payfast_value(passphrase), safe="-_.~")
    # PayFast's documented custom integration uses a lowercase MD5 signature.
    return hashlib.md5(query.encode("utf-8")).hexdigest()

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
