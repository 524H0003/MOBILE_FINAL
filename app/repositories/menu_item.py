"""Menu item repository implementation."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.entities import MenuItem as DomainMenuItem
from app.domain.enums import MenuItemStatus
from app.domain.modifiers import Modifier as DomainModifier
from app.domain.modifiers import ModifierGroup as DomainModifierGroup
from app.orm.models import MenuItem as MenuItemModel
from app.orm.models import Modifier as ModifierModel
from app.orm.models import ModifierGroup as ModifierGroupModel
from app.repositories.base import BaseRepository
from app.repositories.mappers import join_csv, money, money_required, split_csv


class MenuItemRepository(BaseRepository[DomainMenuItem, MenuItemModel]):
    """Menu item repository."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, MenuItemModel)

    async def add(self, entity: DomainMenuItem) -> DomainMenuItem:
        """Add entity and eagerly load modifier_groups with modifiers."""
        model = self._to_model(entity)
        self._session.add(model)
        await self._session.flush()

        # Save modifier groups and modifiers
        for group in entity.modifier_groups:
            group_model = self._modifier_group_to_model(entity, group)
            self._session.add(group_model)
            await self._session.flush()
            for modifier in group.modifiers:
                modifier_model = self._modifier_to_model(group_model, modifier)
                self._session.add(modifier_model)

        await self._session.flush()

        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(MenuItemModel.id == model.id)
        )
        result = await self._session.execute(stmt)
        saved_model = result.scalar_one()
        return self._to_domain(saved_model)

    async def update(self, entity: DomainMenuItem) -> DomainMenuItem:
        """Update entity and eagerly load modifier_groups with modifiers."""
        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(MenuItemModel.id == entity.id)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            raise ValueError(f"Entity {entity.id} not found")
        updated = self._to_model(entity)
        for column in model.__table__.columns:
            setattr(model, column.name, getattr(updated, column.name))

        # Update modifier groups
        await self._update_modifier_groups(model, entity)

        await self._session.flush()
        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(MenuItemModel.id == entity.id)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        saved_model = result.scalar_one()
        return self._to_domain(saved_model)

    async def get(self, entity_id: uuid.UUID) -> DomainMenuItem | None:
        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(MenuItemModel.id == entity_id)
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    def _to_domain(self, model: MenuItemModel) -> DomainMenuItem:
        modifier_groups = []
        if hasattr(model, "modifier_groups") and model.modifier_groups:
            for group_model in model.modifier_groups:
                modifiers = []
                if hasattr(group_model, "modifiers") and group_model.modifiers:
                    for mod_model in group_model.modifiers:
                        modifiers.append(
                            DomainModifier(
                                id=mod_model.id,
                                created_at=mod_model.created_at,
                                updated_at=mod_model.updated_at,
                                group_id=mod_model.group_id,
                                name=mod_model.name,
                                price=money_required(mod_model.price),
                            )
                        )
                modifier_groups.append(
                    DomainModifierGroup(
                        id=group_model.id,
                        created_at=group_model.created_at,
                        updated_at=group_model.updated_at,
                        menu_item_id=group_model.menu_item_id,
                        name=group_model.name,
                        min_selection=group_model.min_selection,
                        max_selection=group_model.max_selection,
                        display_order=group_model.display_order,
                        modifiers=modifiers,
                    )
                )

        return DomainMenuItem(
            id=model.id,
            created_at=model.created_at,
            updated_at=model.updated_at,
            restaurant_id=model.restaurant_id,
            category_id=model.category_id,
            name=model.name,
            description=model.description,
            price=money_required(model.price),
            original_price=money(model.original_price),
            image_url=model.image_url,
            status=model.status,
            is_vegetarian=model.is_vegetarian,
            is_vegan=model.is_vegan,
            is_spicy=model.is_spicy,
            preparation_time_minutes=model.preparation_time_minutes,
            calories=model.calories,
            allergens=split_csv(model.allergens),
            display_order=model.display_order,
            modifier_groups=modifier_groups,
        )

    def _to_model(self, entity: DomainMenuItem) -> MenuItemModel:
        model = MenuItemModel(
            id=entity.id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            restaurant_id=entity.restaurant_id,
            category_id=entity.category_id,
            name=entity.name,
            description=entity.description,
            price=entity.price.amount,
            original_price=entity.original_price.amount if entity.original_price else None,
            image_url=entity.image_url,
            status=entity.status,
            is_vegetarian=entity.is_vegetarian,
            is_vegan=entity.is_vegan,
            is_spicy=entity.is_spicy,
            preparation_time_minutes=entity.preparation_time_minutes,
            calories=entity.calories,
            allergens=join_csv(entity.allergens),
            display_order=entity.display_order,
        )
        return model

    def _modifier_group_to_model(self, entity: DomainMenuItem, group: DomainModifierGroup) -> ModifierGroupModel:
        return ModifierGroupModel(
            id=group.id,
            created_at=group.created_at,
            updated_at=group.updated_at,
            menu_item_id=entity.id,
            name=group.name,
            min_selection=group.min_selection,
            max_selection=group.max_selection,
            display_order=group.display_order,
        )

    def _modifier_to_model(self, group: ModifierGroupModel, modifier: DomainModifier) -> ModifierModel:
        return ModifierModel(
            id=modifier.id,
            created_at=modifier.created_at,
            updated_at=modifier.updated_at,
            group_id=group.id,
            name=modifier.name,
            price=modifier.price.amount,
        )

    async def _update_modifier_groups(self, model: MenuItemModel, entity: DomainMenuItem) -> None:
        """Update modifier groups and their modifiers."""
        existing_group_ids = {g.id for g in model.modifier_groups}
        new_group_ids = {g.id for g in entity.modifier_groups}

        # Delete removed groups (and their modifiers via cascade)
        for group in model.modifier_groups:
            if group.id not in new_group_ids:
                await self._session.delete(group)

        # Update or create modifier groups
        for domain_group in entity.modifier_groups:
            if domain_group.id in existing_group_ids:
                await self._update_existing_group(domain_group)
            else:
                await self._create_new_group(entity, domain_group)

    async def _update_existing_group(self, domain_group: DomainModifierGroup) -> None:
        """Update an existing modifier group and its modifiers."""
        group_stmt = select(ModifierGroupModel).where(ModifierGroupModel.id == domain_group.id)
        group_result = await self._session.execute(group_stmt)
        group_model: ModifierGroupModel | None = group_result.scalar_one_or_none()
        if not group_model:
            return

        group_model.name = domain_group.name
        group_model.min_selection = domain_group.min_selection
        group_model.max_selection = domain_group.max_selection
        group_model.display_order = domain_group.display_order
        group_model.updated_at = domain_group.updated_at

        # Update modifiers - delete existing and recreate
        existing_mod_ids = {m.id for m in group_model.modifiers}
        new_mod_ids = {m.id for m in domain_group.modifiers}

        for mod in group_model.modifiers:
            if mod.id not in new_mod_ids:
                await self._session.delete(mod)

        for modifier in domain_group.modifiers:
            if modifier.id in existing_mod_ids:
                await self._update_existing_modifier(group_model, modifier)
            else:
                await self._create_new_modifier(group_model, modifier)

    async def _update_existing_modifier(self, group_model: ModifierGroupModel, modifier: DomainModifier) -> None:
        """Update an existing modifier."""
        mod_stmt = select(ModifierModel).where(ModifierModel.id == modifier.id)
        mod_result = await self._session.execute(mod_stmt)
        mod_model: ModifierModel | None = mod_result.scalar_one_or_none()
        if mod_model:
            mod_model.name = modifier.name
            mod_model.price = modifier.price.amount
            mod_model.updated_at = modifier.updated_at

    async def _create_new_modifier(self, group_model: ModifierGroupModel, modifier: DomainModifier) -> None:
        """Create a new modifier."""
        mod_model = self._modifier_to_model(group_model, modifier)
        self._session.add(mod_model)

    async def _create_new_group(self, entity: DomainMenuItem, domain_group: DomainModifierGroup) -> None:
        """Create a new modifier group with its modifiers."""
        group_model = self._modifier_group_to_model(entity, domain_group)
        self._session.add(group_model)
        await self._session.flush()
        for modifier in domain_group.modifiers:
            await self._create_new_modifier(group_model, modifier)

    async def get_by_restaurant(
        self, restaurant_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[DomainMenuItem]:
        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(MenuItemModel.restaurant_id == restaurant_id)
            .order_by(MenuItemModel.display_order)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [self._to_domain(model) for model in result.scalars().all()]

    async def get_by_category(
        self, category_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[DomainMenuItem]:
        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(MenuItemModel.category_id == category_id)
            .order_by(MenuItemModel.display_order)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [self._to_domain(model) for model in result.scalars().all()]

    async def get_available_by_restaurant(
        self, restaurant_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[DomainMenuItem]:
        stmt = (
            select(MenuItemModel)
            .options(selectinload(MenuItemModel.modifier_groups).selectinload(ModifierGroupModel.modifiers))
            .where(
                MenuItemModel.restaurant_id == restaurant_id,
                MenuItemModel.status == MenuItemStatus.AVAILABLE,
            )
            .order_by(MenuItemModel.display_order)
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [self._to_domain(model) for model in result.scalars().all()]
