"""Onboarding progress endpoints.

    GET /api/v1/onboarding/me   -> where the owner stopped (defaults if new)
    PUT /api/v1/onboarding/me   -> remember where they are

Deliberately NOT protected by `require_active_subscription`: onboarding
happens BEFORE the person subscribes.

authentication dependency your other owner routers already use (the one that
gives you the signed-in user's uid). If you send me your profile router I
will match it exactly.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import current_user
from app.services import onboarding as onboarding_service

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# Only the steps that can be saved or skipped.
StepName = Literal["profile", "contacts", "vehicle", "tag"]


class OnboardingProgressIn(BaseModel):
    """What the frontend sends. Validated so nothing arbitrary is stored."""

    step: int = Field(default=0, ge=0, le=10)
    saved: list[StepName] = Field(default_factory=list, max_length=10)
    skipped: list[StepName] = Field(default_factory=list, max_length=10)
    started: bool = True
    done: bool = False


@router.get("/me")
def read_my_progress(user=Depends(current_user)):
    """Return this user's onboarding progress."""
    return onboarding_service.get_progress(user["uid"])


@router.put("/me")
def save_my_progress(
    body: OnboardingProgressIn,
    user=Depends(current_user),
):
    """Save this user's onboarding progress (no subscription needed)."""
    return onboarding_service.save_progress(
        user["uid"],
        step=body.step,
        saved=sorted(set(body.saved)),
        skipped=sorted(set(body.skipped)),
        started=body.started,
        done=body.done,
    )