"""Restaurant controller."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast
from uuid import UUID

from fastapi import HTTPException, status
from fastapi_controller import controller

from app.api.controllers.base import BaseController
from app.api.deps.auth import CurrentUser, DbSession
from app.api.deps.pagination import PaginationDep
from app.api.mappers import map_page, map_restaurant, map_restaurant_summary
from app.api.schemas.common import Page
from app.api.schemas.restaurants import (
    RestaurantBusyUpdate,
    RestaurantCreate,
    RestaurantResponse,
    RestaurantSummaryResponse,
    RestaurantUpdate,
)
from app.domain.entities import Restaurant as DomainRestaurant
from app.domain.enums import UserRole
from app.domain.value_objects import Address, Money, PhoneNumber
from app.repositories.restaurant import RestaurantRepository

F = TypeVar("F", bound=Callable[..., Any])


def typed_get(path: str) -> Callable[[F], F]:
    """Typed wrapper for controller.get."""
    return cast(Callable[[F], F], controller.get(path))


def typed_post(path: str) -> Callable[[F], F]:
    """Typed wrapper for controller.post."""
    return cast(Callable[[F], F], controller.post(path))


def typed_patch(path: str) -> Callable[[F], F]:
    """Typed wrapper for controller.patch."""
    return cast(Callable[[F], F], controller.patch(path))


def typed_delete(path: str) -> Callable[[F], F]:
    """Typed wrapper for controller.delete."""
    return cast(Callable[[F], F], controller.delete(path))


class RestaurantController(BaseController):  # type: ignore[misc]
    """Restaurant endpoints."""

    @typed_get("/")
    async def list_restaurants(self, session: DbSession, pagination: PaginationDep) -> Page[RestaurantSummaryResponse]:
        """List active restaurants."""
        repo = RestaurantRepository(session)
        restaurants = await repo.list_active(limit=pagination.limit, offset=pagination.offset)
        return map_page(
            [map_restaurant_summary(r) for r in restaurants],
            total=len(restaurants),
            page=pagination.page,
            size=pagination.limit,
        )

    @typed_get("/owner/mine")
    async def list_my_restaurants(self, session: DbSession, current_user: CurrentUser) -> list[RestaurantResponse]:
        """List restaurants owned by the current user."""
        if current_user.role not in (UserRole.OWNER, UserRole.STAFF):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only owners can view their restaurants",
            )
        repo = RestaurantRepository(session)
        restaurants = await repo.get_by_owner(current_user.id)
        return [map_restaurant(r) for r in restaurants]

    @typed_get("/{restaurant_id}")
    async def get_restaurant(self, restaurant_id: UUID, session: DbSession) -> RestaurantResponse:
        """Get restaurant by ID."""
        repo = RestaurantRepository(session)
        restaurant = await repo.get(restaurant_id)
        if not restaurant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found")
        return map_restaurant(restaurant)

    @typed_post("/")
    async def create_restaurant(
        self, request: RestaurantCreate, session: DbSession, current_user: CurrentUser
    ) -> RestaurantResponse:
        """Create a restaurant (owner only)."""
        if current_user.role != UserRole.OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owners can create restaurants")

        repo = RestaurantRepository(session)
        restaurant = DomainRestaurant(
            owner_id=current_user.id,
            name=request.name,
            description=request.description,
            address=Address(
                street=request.street,
                ward=request.ward,
                district=request.district,
                city=request.city,
                latitude=float(request.latitude) if request.latitude is not None else None,
                longitude=float(request.longitude) if request.longitude is not None else None,
            ),
            phone=PhoneNumber(number=PhoneNumber.normalize(request.phone)),
            email=request.email or f"restaurant-{current_user.id}@example.com",
            opening_time=request.opening_time.strftime("%H:%M"),
            closing_time=request.closing_time.strftime("%H:%M"),
            min_order_amount=Money(amount=request.minimum_order_amount),
            delivery_fee=Money(amount=request.delivery_fee),
            delivery_radius_km=request.delivery_radius_km,
            status=request.status,
            busy_until=request.busy_until,
            weekly_schedule=request.weekly_schedule,
            custom_schedules=request.custom_schedules,
            logo_url=request.avatar_url,
            cover_image_url=request.cover_url,
        )
        saved = await repo.add(restaurant)
        return map_restaurant(saved)

    @typed_patch("/{restaurant_id}")
    async def update_restaurant(
        self,
        restaurant_id: UUID,
        request: RestaurantUpdate,
        session: DbSession,
        current_user: CurrentUser,
    ) -> RestaurantResponse:
        """Update a restaurant (owner only)."""
        repo = RestaurantRepository(session)
        restaurant = await repo.get(restaurant_id)
        if not restaurant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found")
        if restaurant.owner_id != current_user.id and current_user.role != UserRole.STAFF:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")


        update_data = request.model_dump(exclude_unset=True)
        field_aliases = {
            "minimum_order_amount": "min_order_amount",
            "avatar_url": "logo_url",
            "cover_url": "cover_image_url",
        }
        money_fields = {"min_order_amount", "delivery_fee"}
        time_fields = {"opening_time", "closing_time"}

        for field_name, value in update_data.items():
            target_name = field_aliases.get(field_name, field_name)
            if not hasattr(restaurant, target_name):
                continue
            if value is not None:
                if target_name in time_fields:
                    value = value.strftime("%H:%M")
                elif target_name in money_fields:
                    value = Money(amount=value)
                elif target_name == "phone":
                    value = PhoneNumber(number=PhoneNumber.normalize(value))
            setattr(restaurant, target_name, value)
        restaurant.touch()
        saved = await repo.update(restaurant)
        return map_restaurant(saved)

    @typed_patch("/{restaurant_id}/busy")
    async def set_busy(
        self,
        restaurant_id: UUID,
        request: RestaurantBusyUpdate,
        session: DbSession,
        current_user: CurrentUser,
    ) -> RestaurantResponse:
        """Set or clear the temporary busy window (15m/30m/1h/1day)."""
        repo = RestaurantRepository(session)
        restaurant = await repo.get(restaurant_id)
        if not restaurant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found")
        if restaurant.owner_id != current_user.id and current_user.role != UserRole.STAFF:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        restaurant.busy_until = (
            datetime.utcnow() + timedelta(minutes=request.duration_minutes)
            if request.duration_minutes is not None
            else None
        )
        restaurant.touch()
        return map_restaurant(await repo.update(restaurant))

    @typed_delete("/{restaurant_id}")
    async def delete_restaurant(
        self, restaurant_id: UUID, session: DbSession, current_user: CurrentUser
    ) -> dict[str, str]:
        """Delete a restaurant (owner only)."""
        repo = RestaurantRepository(session)
        restaurant = await repo.get(restaurant_id)
        if not restaurant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found")
        if restaurant.owner_id != current_user.id and current_user.role != UserRole.STAFF:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        await repo.remove(restaurant_id)
        return {"message": "Restaurant deleted"}
