from pydantic import BaseModel, Field
from typing import Literal

PlanName = Literal["safelink_monthly"]

class CheckoutRequest(BaseModel):
    plan_id: PlanName = "safelink_monthly"
    accepted_recurring_terms: bool
    terms_version: str = Field(min_length=1, max_length=40)

class CancelSubscriptionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
