"""Modifier API schemas."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.value_objects import Money


class ModifierCreate(BaseModel):
    """Modifier creation schema."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    price: Decimal = Field(default=0, ge=0)


class ModifierGroupCreate(BaseModel):
    """Modifier group creation schema."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    min_selection: int = Field(default=0, ge=0)
    max_selection: int = Field(default=1, ge=1)
    display_order: int = Field(default=0, ge=0)
    modifiers: list[ModifierCreate] = Field(default_factory=list)


class ModifierResponse(BaseModel):
    """Modifier response schema."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    name: str = Field(..., min_length=1, max_length=200)
    price: Money


class ModifierGroupResponse(BaseModel):
    """Modifier group response schema."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    name: str = Field(..., min_length=1, max_length=200)
    min_selection: int = Field(default=0, ge=0)
    max_selection: int = Field(default=1, ge=1)
    display_order: int = Field(default=0, ge=0)
    modifiers: list[ModifierResponse]
