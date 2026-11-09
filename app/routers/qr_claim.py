"""Claim a physical tag by scanning it. Owner endpoints: no subscription needed.

    POST /api/v1/qr-tags/claim/lookup    token -> tag details (changes nothing)
    POST /api/v1/qr-tags/claim/confirm   token -> links the tag to the user
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import current_user
from app.services import tag_claim

router = APIRouter(tags=["QR tag claiming"])


class ClaimLookup(BaseModel):
    # Length is checked in the service so a bad code gets a friendly 404.
    token: str = Field(max_length=500)


class ClaimConfirm(BaseModel):
    token: str = Field(max_length=500)
    label: str = Field(default="", max_length=80)


@router.post("/qr-tags/claim/lookup")
def lookup_tag(body: ClaimLookup, user: dict = Depends(current_user)):
    return tag_claim.lookup(user["uid"], body.token)


@router.post("/qr-tags/claim/confirm")
def confirm_tag(body: ClaimConfirm, user: dict = Depends(current_user)):
    return tag_claim.confirm(user, body.token, body.label)
