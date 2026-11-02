"""Firebase Admin and Firestore initialisation."""

import json
import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore, auth

from app.config import settings


def _initialise_firebase():
    """Initialise Firebase using JSON, a credentials file, or host credentials."""

    # Reuse Firebase if it has already been initialised.
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    # Read the optional service-account JSON setting.
    raw_json = (
        settings.firebase_service_account_json
        or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        or ""
    ).strip()

    if raw_json:
        # If a path was entered instead of JSON, load the file.
        if raw_json.endswith(".json") and "\n" not in raw_json:
            path = Path(raw_json).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path

            if not path.is_file():
                raise RuntimeError(
                    "Firebase JSON setting looks like a filename, "
                    "but the file does not exist. Empty "
                    "FIREBASE_SERVICE_ACCOUNT_JSON and use "
                    "GOOGLE_APPLICATION_CREDENTIALS."
                )

            cred = credentials.Certificate(str(path))
        else:
            try:
                info = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "FIREBASE_SERVICE_ACCOUNT_JSON is invalid. "
                    "Use valid JSON or leave it empty when using "
                    "GOOGLE_APPLICATION_CREDENTIALS."
                ) from exc

            if not isinstance(info, dict):
                raise RuntimeError(
                    "Firebase service-account JSON must be an object."
                )

            cred = credentials.Certificate(info)

    else:
        # Load the local service-account file.
        configured_path = (
            settings.google_application_credentials
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or ""
        ).strip()

        if configured_path:
            path = Path(configured_path).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path

            if not path.is_file():
                raise RuntimeError(
                    f"Firebase credentials file not found: {path}"
                )

            cred = credentials.Certificate(str(path))
        else:
            # For hosted environments configured with Application Default
            # Credentials.
            cred = credentials.ApplicationDefault()

    project_id = (settings.firebase_project_id or "").strip()
    options = {"projectId": project_id} if project_id else {}

    return firebase_admin.initialize_app(cred, options or None)


_firebase_app = _initialise_firebase()
db = firestore.client(app=_firebase_app)


def verify_firebase_id_token(token: str) -> dict:
    """Verify the Firebase ID token; never trust a client-supplied UID."""
    return auth.verify_id_token(token, app=_firebase_app)
