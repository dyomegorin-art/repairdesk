"""Shape of an intake payload. Limits match the database columns, so a value that would
overflow a column is rejected with 400 instead of failing the insert with a 500.

Unknown extra fields are allowed (sources differ); the raw body is kept anyway.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_BASE = ConfigDict(extra="allow", strict=True)


class ContactIn(BaseModel):
    model_config = _BASE
    display_name: str | None = Field(default=None, max_length=256)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=256)


class UnitIn(BaseModel):
    model_config = _BASE
    appliance_type: str | None = Field(default=None, max_length=64)
    brand: str | None = Field(default=None, max_length=64)
    problem: str | None = Field(default=None, max_length=1000)
    power_source: Literal["ELECTRIC", "GAS", "HARDWIRED"] | None = None
    customer_supplied_part: bool | None = None


class IntakePayload(BaseModel):
    model_config = _BASE
    source_system: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_channel: str | None = Field(default=None, max_length=32)
    contact: ContactIn | None = None
    location_type: Literal["RESIDENTIAL", "COMMERCIAL"] | None = None
    address: str | None = Field(default=None, max_length=512)
    district: str | None = Field(default=None, max_length=128)
    units: list[UnitIn] | None = Field(default=None, max_length=50)
    hazards: list[str] | None = Field(default=None, max_length=20)
    requested_work: list[str] | None = Field(default=None, max_length=20)
    availability: str | None = Field(default=None, max_length=256)
    notes: str | None = Field(default=None, max_length=2000)
