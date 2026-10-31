"""Write-action payload schemas. Anything not listed here cannot be proposed or executed."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


class SendMessagePayload(BaseModel):
    to: str = Field(min_length=1, max_length=120)
    channel: Literal["message"] = "message"
    text: str = Field(min_length=1, max_length=600)

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class BookTablePayload(BaseModel):
    restaurant_id: str
    restaurant_name: str
    start: datetime
    end: datetime
    party_size: int = Field(ge=1, le=12)
    link: str | None = None


class PayPayload(BaseModel):
    payee: str
    amount: float = Field(gt=0, le=100_000)
    currency: str = Field(min_length=3, max_length=3)
    reference: str = ""


_MODELS: dict[str, type[BaseModel]] = {
    "send_message": SendMessagePayload,
    "book_table": BookTablePayload,
    "pay": PayPayload,
}


class InvalidAction(Exception):
    pass


def validate_payload(kind: str, payload: dict) -> dict:
    model = _MODELS.get(kind)
    if model is None:
        raise InvalidAction(f"Unknown action kind: {kind!r}")
    try:
        return model.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise InvalidAction(f"Invalid {kind} payload: {exc.errors()[0]['msg']}") from exc
