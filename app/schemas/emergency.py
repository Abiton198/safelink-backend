"""Validated request models for SafeLink emergency-profile endpoints."""
from datetime import date
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator

class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date
    emergency_summary: str = Field(default="", max_length=1000)
    medical_notes: str = Field(default="", max_length=1000)
    preferred_language: str = Field(default="en", max_length=20)

class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    relationship: str = Field(default="", max_length=60)
    phone: str = Field(min_length=5, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    priority: int = Field(default=1, ge=1, le=10)
    share_with_responder: bool = True

class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    relationship: Optional[str] = Field(default=None, max_length=60)
    phone: Optional[str] = Field(default=None, min_length=5, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    share_with_responder: Optional[bool] = None

class VehicleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    make: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=60)
    colour: str = Field(default="", max_length=40)
    registration_number: str = Field(default="", max_length=30)
    tracker_provider: str = Field(default="", max_length=100)
    tracker_reference: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)

class VehicleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(default=None, min_length=1, max_length=80)
    make: Optional[str] = Field(default=None, max_length=60)
    model: Optional[str] = Field(default=None, max_length=60)
    colour: Optional[str] = Field(default=None, max_length=40)
    registration_number: Optional[str] = Field(default=None, max_length=30)
    tracker_provider: Optional[str] = Field(default=None, max_length=100)
    tracker_reference: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=500)

class QRCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="SafeLink tag", min_length=1, max_length=80)

class DOBVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_of_birth: date

class EmergencyAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(min_length=40, max_length=200)
