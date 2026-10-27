"""Firebase bearer-token authentication dependency."""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.firebase import verify_firebase_id_token

bearer = HTTPBearer(auto_error=False)

def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        return verify_firebase_id_token(credentials.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token.")
