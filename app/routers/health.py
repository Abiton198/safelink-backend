from fastapi import APIRouter
from datetime import datetime, timezone
router = APIRouter(tags=["Health"])

@router.get("/health")
def health():
    return {"status": "ok", "service": "safelink-api", "time": datetime.now(timezone.utc).isoformat()}
