"""Modifier and ModifierGroup entities."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import Field

from app.domain.base import DomainEntity
from app.domain.value_objects import Money


class Modifier(DomainEntity):
    """Individual option for a menu item."""

    group_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=200)
    price: Money


class ModifierGroup(DomainEntity):
    """Group of modifiers (e.g., Size, Toppings)."""

    menu_item_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=200)
    min_selection: int = Field(default=0, ge=0)
    max_selection: int = Field(default=1, ge=1)
    display_order: int = Field(default=0, ge=0)
    modifiers: list[Modifier] = Field(default_factory=list)
