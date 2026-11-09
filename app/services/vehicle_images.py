"""Vehicle photos: private storage in Firebase Storage + a list on the vehicle.

WHERE THINGS LIVE
-----------------
  Firestore  users/{name}__{uid}/vehicles/{vehicleId}  ->  `images` list:
                 [{image_id, path, content_type, size, created_at}, ...]
  Storage    vehicle_images/{uid}/{vehicleId}/{imageId}.jpg   (private bucket)

Rules enforced here (the frontend limits are only for convenience):
  * at most MAX_IMAGES_PER_VEHICLE photos per vehicle,
  * a person can only reach vehicles under their OWN document,
  * every upload is validated and re-encoded (see image_clean.py),
  * the bucket stays PRIVATE. Photos are shown through short-lived signed
    links, and storage paths are never sent to the browser.

Photos ARE shown to a responder, but only after a successful date-of-birth
check on an active tag with an active subscription, and only as links that
expire after 10 minutes (see `responder_photo_urls`).
"""

import logging
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from firebase_admin import storage
from google.cloud import firestore

from app.config import settings
from app.core.firebase import db
from app.services import userstore
from app.services.image_clean import clean_image

logger = logging.getLogger("uvicorn.error")

MAX_IMAGES_PER_VEHICLE = 5          # keep in step with the frontend constant
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # the browser shrinks photos long before this
URL_LIFETIME = timedelta(hours=1)             # owner viewing their own photos
RESPONDER_URL_LIFETIME = timedelta(minutes=10)  # same as a scan's access token


def _bucket():
    if not settings.firebase_storage_bucket:
        raise HTTPException(
            status_code=503,
            detail="Photo storage is not configured. Missing: FIREBASE_STORAGE_BUCKET",
        )
    return storage.bucket(settings.firebase_storage_bucket)


def _signed_url(blob, lifetime: timedelta = URL_LIFETIME) -> str | None:
    """A temporary link to a private photo; None if it cannot be signed."""
    try:
        return blob.generate_signed_url(version="v4", expiration=lifetime, method="GET")
    except Exception:  # noqa: BLE001 - signing needs a service-account key
        logger.exception("Could not create a signed URL for a vehicle photo")
        return None


def public_images(vehicle: dict) -> list[dict]:
    """Photos of a vehicle as the browser may see them: id + temporary link."""
    entries = vehicle.get("images") or []
    if not entries:
        return []

    bucket = _bucket()
    return [
        {"image_id": e["image_id"], "url": _signed_url(bucket.blob(e["path"]))}
        for e in entries
        if e.get("image_id") and e.get("path")
    ]


def responder_photo_urls(vehicle: dict) -> list[str]:
    """Temporary photo links for someone who has just passed a tag scan.

    Differences from `public_images`: the links last only 10 minutes, and it
    NEVER raises. If storage is not configured or signing fails, the responder
    simply gets no photos. Emergency details must never fail because of a picture.
    """
    entries = vehicle.get("images") or []
    if not entries or not settings.firebase_storage_bucket:
        return []

    try:
        bucket = _bucket()
        urls = []
        for entry in entries:
            path = entry.get("path")
            if not path:
                continue
            url = _signed_url(bucket.blob(path), RESPONDER_URL_LIFETIME)
            if url:
                urls.append(url)
        return urls
    except Exception:  # noqa: BLE001
        logger.exception("Could not prepare vehicle photos for a responder")
        return []


@firestore.transactional
def _append_image(transaction, vehicle_ref, entry: dict) -> bool:
    """Add a photo entry unless the vehicle is gone or full.

    A transaction, so two uploads at once cannot push a vehicle past the limit.
    """
    snap = vehicle_ref.get(transaction=transaction)
    if not snap.exists:
        return False

    images = list((snap.to_dict() or {}).get("images") or [])
    if len(images) >= MAX_IMAGES_PER_VEHICLE:
        return False

    images.append(entry)
    transaction.update(vehicle_ref, {"images": images, "updated_at": userstore.now_utc()})
    return True


@firestore.transactional
def _remove_image(transaction, vehicle_ref, image_id: str):
    """Remove one photo entry. Returns its storage path, or None if not found."""
    snap = vehicle_ref.get(transaction=transaction)
    if not snap.exists:
        return None

    images = list((snap.to_dict() or {}).get("images") or [])
    removed = next((e for e in images if e.get("image_id") == image_id), None)
    if removed is None:
        return None

    kept = [e for e in images if e.get("image_id") != image_id]
    transaction.update(vehicle_ref, {"images": kept, "updated_at": userstore.now_utc()})
    return removed.get("path")


def _own_vehicle_ref(uid: str, vehicle_id: str):
    """This person's vehicle document. 404 (not 403) for anything else, so
    other people's vehicles are not revealed."""
    user_ref = userstore.find_user_ref(uid)
    if user_ref is None:
        raise HTTPException(status_code=404, detail="Vehicle not found.")
    return userstore.vehicles_col(user_ref).document(vehicle_id)


def add_image(uid: str, vehicle_id: str, upload: UploadFile) -> dict:
    """Validate, store and record one photo. Returns {image_id, url}."""
    vehicle_ref = _own_vehicle_ref(uid, vehicle_id)
    snap = vehicle_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Vehicle not found.")

    if len((snap.to_dict() or {}).get("images") or []) >= MAX_IMAGES_PER_VEHICLE:
        raise HTTPException(
            status_code=409,
            detail=f"A vehicle can have at most {MAX_IMAGES_PER_VEHICLE} photos.",
        )

    raw = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That photo is too large.")

    try:
        jpeg = clean_image(raw)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    image_id = uuid4().hex
    path = f"vehicle_images/{uid}/{vehicle_id}/{image_id}.jpg"

    blob = _bucket().blob(path)
    blob.upload_from_string(jpeg, content_type="image/jpeg")

    entry = {
        "image_id": image_id,
        "path": path,
        "content_type": "image/jpeg",
        "size": len(jpeg),
        "created_at": userstore.now_utc(),
    }

    try:
        added = _append_image(db.transaction(), vehicle_ref, entry)
    except Exception:
        blob.delete()  # do not leave an orphan file behind
        raise

    if not added:
        blob.delete()
        raise HTTPException(
            status_code=409,
            detail=f"A vehicle can have at most {MAX_IMAGES_PER_VEHICLE} photos.",
        )

    return {"image_id": image_id, "url": _signed_url(blob)}


def delete_image(uid: str, vehicle_id: str, image_id: str) -> None:
    """Remove one photo (record first, then the stored file)."""
    vehicle_ref = _own_vehicle_ref(uid, vehicle_id)
    path = _remove_image(db.transaction(), vehicle_ref, image_id)

    if path is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    try:
        _bucket().blob(path).delete()
    except Exception:  # noqa: BLE001 - the record is already gone; just log it
        logger.exception("Could not delete a stored vehicle photo")


def delete_all_images(vehicle: dict) -> None:
    """Best-effort cleanup of every stored photo of a vehicle being deleted."""
    entries = vehicle.get("images") or []
    if not entries or not settings.firebase_storage_bucket:
        return

    bucket = _bucket()
    for entry in entries:
        try:
            bucket.blob(entry["path"]).delete()
        except Exception:  # noqa: BLE001
            logger.exception("Could not delete a stored vehicle photo")
