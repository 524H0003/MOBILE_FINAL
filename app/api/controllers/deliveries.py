"""Delivery controller."""

from collections.abc import Callable
from typing import Any, TypeVar, cast
from uuid import UUID

from fastapi import HTTPException, status
from fastapi_controller import controller

from app.api.controllers.base import BaseController
from app.api.deps.auth import CurrentUser, DbSession
from app.api.mappers import map_delivery
from app.api.schemas.deliveries import (
    DeliveryAddress,
    DeliveryDispatchRequest,
    DeliveryQuoteRequest,
    DeliveryQuoteResponse,
    DeliveryResponse,
    DeliveryTrackingUpdate,
)
from app.domain.enums import UserRole
from app.domain.value_objects import Address, Coordinates
from app.services.delivery import DeliveryService

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


class DeliveryController(BaseController):  # type: ignore[misc]
    """Delivery endpoints."""

    @typed_post("/quote")
    async def quote_delivery(
        self, request: DeliveryQuoteRequest, session: DbSession, current_user: CurrentUser
    ) -> DeliveryQuoteResponse:
        """Quote shipping fee from AhaMove/Lalamove before payment."""
        service = DeliveryService(session)

        def to_address(value: DeliveryAddress) -> Address:
            return Address(
                street=value.street,
                ward=value.ward,
                district=value.district,
                city=value.city,
                latitude=float(value.latitude) if value.latitude is not None else None,
                longitude=float(value.longitude) if value.longitude is not None else None,
            )

        pickup = to_address(request.pickup)
        dropoff = to_address(request.dropoff)
        quote = await service.get_quote(
            pickup=pickup,
            dropoff=dropoff,
            provider=request.provider,
            pickup_coordinates=(
                Coordinates(latitude=pickup.latitude, longitude=pickup.longitude)
                if pickup.latitude is not None and pickup.longitude is not None
                else None
            ),
            dropoff_coordinates=(
                Coordinates(latitude=dropoff.latitude, longitude=dropoff.longitude)
                if dropoff.latitude is not None and dropoff.longitude is not None
                else None
            ),
        )
        return DeliveryQuoteResponse.model_validate(quote)

    @typed_get("/{order_id}/delivery")
    async def get_delivery(self, order_id: UUID, session: DbSession, current_user: CurrentUser) -> DeliveryResponse:
        """Get delivery for an order."""
        service = DeliveryService(session)
        delivery = await service.get_delivery_by_order(order_id)
        if not delivery:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
        return map_delivery(delivery)

    @typed_post("/{order_id}/delivery/dispatch")
    async def dispatch_delivery(
        self,
        order_id: UUID,
        request: DeliveryDispatchRequest,
        session: DbSession,
        current_user: CurrentUser,
    ) -> DeliveryResponse:
        """Manually dispatch delivery."""
        if current_user.role not in (UserRole.OWNER, UserRole.STAFF):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
        service = DeliveryService(session)
        try:
            delivery = await service.auto_dispatch(order_id, request.provider)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return map_delivery(delivery)

    @typed_patch("/{order_id}/delivery/tracking")
    async def update_tracking(
        self,
        order_id: UUID,
        request: DeliveryTrackingUpdate,
        session: DbSession,
        current_user: CurrentUser,
    ) -> DeliveryResponse:
        """Update delivery tracking."""
        service = DeliveryService(session)
        try:
            delivery = await service.update_tracking(
                order_id,
                status=request.status,
                driver_name=request.driver_name,
                driver_phone=request.driver_phone,
                driver_vehicle=request.driver_vehicle,
                driver_license_plate=request.driver_license_plate,
                tracking_url=request.tracking_url,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return map_delivery(delivery)
