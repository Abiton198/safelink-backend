"""SafeLink API entry point."""
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import health, subscriptions, payfast, emergency, public_emergency, onboarding

logger = logging.getLogger("uvicorn.error")

# Largest request body accepted. Vehicle photos are limited to 8 MB by the
# photo service, plus a little room for the form wrapper.
MAX_BODY_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Keep scan access tokens out of the logs
# ---------------------------------------------------------------------------
# GET /public/emergency?access_token=... carries a short-lived secret in the
# address, and uvicorn's access log prints the whole address. This filter
# replaces the token in log lines with [redacted].
_TOKEN_IN_URL = re.compile(r"(access_token=)[^&\s\"]+")


class RedactAccessToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn access records: (client, method, full_path, http_version, status)
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str):
                args[2] = _TOKEN_IN_URL.sub(r"\1[redacted]", args[2])
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactAccessToken())


# ---------------------------------------------------------------------------
# Startup summary (never prints secrets)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    missing_payfast = [
        name
        for name, value in (
            ("PAYFAST_MERCHANT_ID", settings.payfast_merchant_id),
            ("PAYFAST_MERCHANT_KEY", settings.payfast_merchant_key),
            ("PAYFAST_PASSPHRASE", settings.payfast_passphrase),
        )
        if not value
    ]
    logger.info(
        "SafeLink API starting: env=%s | PayFast=%s | photo storage=%s | CORS=%s",
        settings.app_env,
        "SANDBOX" if settings.payfast_sandbox else "LIVE",
        "configured" if getattr(settings, "firebase_storage_bucket", "") else "NOT configured",
        ", ".join(settings.cors_origins),
    )
    if missing_payfast:
        logger.warning("PayFast settings missing: %s", ", ".join(missing_payfast))
    yield


app = FastAPI(
    title="SafeLink API",
    version="0.1.0",
    description="SafeLink account, emergency profile, QR access, subscription, and payment foundation.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Safety net around every request
# ---------------------------------------------------------------------------
# Added BEFORE the CORS middleware below, so it sits INSIDE it. That matters:
# without it, an unexpected crash makes a plain 500 page with no CORS headers,
# and the browser reports a confusing "blocked by CORS" error instead of the
# real server error. Now the browser sees an ordinary 500 and the real
# traceback is in the Render log.
@app.middleware("http")
async def safety_net(request: Request, call_next):
    # 1) Refuse oversized uploads early (before they are read into memory).
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "Request is too large."}, status_code=413)

    # 2) Turn unexpected crashes into a clean JSON 500 (and log them).
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - HTTPExceptions never reach here
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse(
            {"detail": "Something went wrong on our side. Please try again."},
            status_code=500,
        )

    # 3) API answers hold private data and temporary links: never cache them,
    #    and never leak the page address as a referrer.
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# Added LAST, so it is the OUTERMOST layer and puts CORS headers on every
# response, including errors. Restrict origins in production to the exact
# deployed frontend domains (CORS_ORIGINS_CSV).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,  # browsers remember the preflight answer for 10 minutes
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")
app.include_router(payfast.router, prefix="/api/v1")
app.include_router(emergency.router, prefix="/api/v1")
app.include_router(public_emergency.router, prefix="/api/v1")
app.include_router(onboarding.router, prefix="/api/v1")


@app.get("/")
def root():
    return {"service": "SafeLink API", "version": "0.1.0", "docs": "/docs"}