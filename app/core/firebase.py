"""Firebase Admin and Firestore initialisation."""
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore, auth
from app.config import settings

def _initialise_firebase():
    if firebase_admin._apps:
        return firebase_admin.get_app()

    if settings.firebase_service_account_json:
        info = json.loads(settings.firebase_service_account_json)
        cred = credentials.Certificate(info)
    elif settings.google_application_credentials or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        path = settings.google_application_credentials or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        cred = credentials.Certificate(path)
    else:
        # Uses Application Default Credentials in hosted environments.
        cred = credentials.ApplicationDefault()

    options = {}
    if settings.firebase_project_id:
        options["projectId"] = settings.firebase_project_id
    return firebase_admin.initialize_app(cred, options or None)

_firebase_app = _initialise_firebase()
db = firestore.client(app=_firebase_app)

def verify_firebase_id_token(token: str) -> dict:
    """Verify a Firebase ID token. Never accept a UID supplied in request JSON."""
    return auth.verify_id_token(token, app=_firebase_app)
