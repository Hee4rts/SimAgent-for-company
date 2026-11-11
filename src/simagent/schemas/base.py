"""Shared Pydantic configuration, identifiers and timestamps."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")]
Positive = Annotated[float, Field(gt=0)]
JsonPointer = Annotated[str, Field(pattern=r"^(?:/(?:[^~]|~[01])*)?$")]


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
