"""SafeLink API entry point."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import health, subscriptions, payfast, emergency, public_emergency

app = FastAPI(
    title="SafeLink API",
    version="0.1.0",
    description="SafeLink account, emergency profile, QR access, subscription, and payment foundation.",
)

# Restrict these origins in production to the exact deployed frontend domains.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")
app.include_router(payfast.router, prefix="/api/v1")
app.include_router(emergency.router, prefix="/api/v1")
app.include_router(public_emergency.router, prefix="/api/v1")

@app.get("/")
def root():
    return {"service": "SafeLink API", "version": "0.1.0", "docs": "/docs"}
