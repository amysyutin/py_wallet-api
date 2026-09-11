from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TelegramSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    timezone: str
    daily_at: time
    language: Literal["ru", "en"]
    alert_threshold_percent: float | None
    allows_write_to_pm: bool


class TelegramSettingsUpdate(BaseModel):
    enabled: bool | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    daily_at: time | None = None
    language: Literal["ru", "en"] | None = None
    alert_threshold_percent: float | None = Field(default=None, ge=0.1, le=1000)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "TelegramSettingsUpdate":
        for field_name in ("enabled", "timezone", "daily_at", "language"):
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value
