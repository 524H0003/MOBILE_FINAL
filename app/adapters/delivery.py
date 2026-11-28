"""Delivery provider adapters (mock implementations)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import httpx

from app.config.settings import get_settings
from app.domain.enums import DeliveryProvider, DeliveryStatus
from app.domain.value_objects import Address, Coordinates


@dataclass(frozen=True, slots=True)
class DeliveryQuote:
    """Delivery quote from provider."""

    provider: DeliveryProvider
    fee: Decimal
    distance_km: Decimal
    estimated_duration_minutes: int


@dataclass(frozen=True, slots=True)
class DeliveryBooking:
    """Delivery booking result from provider."""

    provider_order_id: str
    status: DeliveryStatus
    driver_name: str | None = None
    driver_phone: str | None = None
    driver_vehicle: str | None = None
    driver_license_plate: str | None = None
    tracking_url: str | None = None
    estimated_pickup_at: str | None = None
    estimated_dropoff_at: str | None = None


class DeliveryProviderAdapter(Protocol):
    """Protocol for delivery provider adapters."""

    async def get_quote(
        self,
        *,
        pickup: Address,
        dropoff: Address,
        pickup_coordinates: Coordinates | None = None,
        dropoff_coordinates: Coordinates | None = None,
    ) -> DeliveryQuote:
        """Get delivery quote."""
        ...

    async def create_booking(
        self,
        *,
        order_id: uuid.UUID,
        pickup: Address,
        dropoff: Address,
        pickup_coordinates: Coordinates | None = None,
        dropoff_coordinates: Coordinates | None = None,
        cod_amount: Decimal | None = None,
        notes: str | None = None,
    ) -> DeliveryBooking:
        """Create delivery booking."""
        ...


class MockDeliveryAdapter:
    """Mock delivery provider adapter for development/testing."""

    def __init__(self, provider: DeliveryProvider) -> None:
        self.provider = provider

    async def get_quote(
        self,
        *,
        pickup: Address,
        dropoff: Address,
        pickup_coordinates: Coordinates | None = None,
        dropoff_coordinates: Coordinates | None = None,
    ) -> DeliveryQuote:
        """Get mock delivery quote based on distance."""
        # Calculate distance if coordinates available
        if pickup_coordinates and dropoff_coordinates:
            distance = Decimal(str(pickup_coordinates.distance_to(dropoff_coordinates)))
        else:
            distance = Decimal("3.0")  # default 3km

        # Fee = base fee + distance fee
        base_fee = Decimal("15000")
        per_km = Decimal("5000")
        fee = base_fee + (distance * per_km)

        # Duration = 10 min base + 5 min per km
        duration = int(10 + (distance * Decimal("5")))

        return DeliveryQuote(
            provider=self.provider,
            fee=fee.quantize(Decimal("1")),
            distance_km=distance.quantize(Decimal("0.1")),
            estimated_duration_minutes=duration,
        )

    async def create_booking(
        self,
        *,
        order_id: uuid.UUID,
        pickup: Address,
        dropoff: Address,
        pickup_coordinates: Coordinates | None = None,
        dropoff_coordinates: Coordinates | None = None,
        cod_amount: Decimal | None = None,
        notes: str | None = None,
    ) -> DeliveryBooking:
        """Create mock delivery booking."""
        return DeliveryBooking(
            provider_order_id=f"MOCK-{self.provider.value}-{order_id.hex[:12].upper()}",
            status=DeliveryStatus.DRIVER_ASSIGNED,
            driver_name="Nguyễn Văn A (Mock)",
            driver_phone="0901234567",
            driver_vehicle="Xe máy",
            driver_license_plate="51B-12345",
            tracking_url=f"https://tracking.mock.com/{self.provider.value.lower()}/{order_id}",
        )


class HttpDeliveryAdapter:
    """HTTP adapter for real AhaMove/Lalamove APIs."""

    def __init__(self, provider: DeliveryProvider) -> None:
        self.provider = provider
        settings = get_settings()
        if provider == DeliveryProvider.AHAMOVE:
            self.base_url = settings.ahamove_base_url
            self.api_key = settings.ahamove_api_key
            self.api_secret = settings.ahamove_api_secret
        else:
            self.base_url = settings.lalamove_base_url
            self.api_key = settings.lalamove_api_key
            self.api_secret = settings.lalamove_api_secret

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-API-Secret": self.api_secret,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _coordinates(coordinates: Coordinates | None) -> dict[str, float]:
        if coordinates is None:
            return {}
        return {"lat": coordinates.latitude, "lng": coordinates.longitude}

    async def get_quote(
        self,
        *,
        pickup: Address,
        dropoff: Address,
        pickup_coordinates: Coordinates | None = None,
        dropoff_coordinates: Coordinates | None = None,
    ) -> DeliveryQuote:
        """Request a quote from the configured provider endpoint."""
        payload = {
            "pickup": {"address": pickup.full_address(), **self._coordinates(pickup_coordinates)},
            "dropoff": {"address": dropoff.full_address(), **self._coordinates(dropoff_coordinates)},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{self.base_url}/quote", json=payload, headers=self._headers())
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        return DeliveryQuote(
            provider=self.provider,
            fee=Decimal(str(data["fee"])),
            distance_km=Decimal(str(data.get("distance_km", 0))),
            estimated_duration_minutes=int(data.get("estimated_duration_minutes", 0)),
        )

    async def create_booking(
        self,
        *,
        order_id: uuid.UUID,
        pickup: Address,
        dropoff: Address,
        pickup_coordinates: Coordinates | None = None,
        dropoff_coordinates: Coordinates | None = None,
        cod_amount: Decimal | None = None,
        notes: str | None = None,
    ) -> DeliveryBooking:
        """Create a delivery booking with the configured provider."""
        payload = {
            "order_id": str(order_id),
            "pickup": {"address": pickup.full_address(), **self._coordinates(pickup_coordinates)},
            "dropoff": {"address": dropoff.full_address(), **self._coordinates(dropoff_coordinates)},
            "cod_amount": str(cod_amount) if cod_amount is not None else None,
            "notes": notes,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{self.base_url}/bookings", json=payload, headers=self._headers())
            response.raise_for_status()
            data = response.json()
        return DeliveryBooking(
            provider_order_id=str(data["provider_order_id"]),
            status=DeliveryStatus(data.get("status", DeliveryStatus.DRIVER_ASSIGNED.value)),
            driver_name=data.get("driver_name"),
            driver_phone=data.get("driver_phone"),
            driver_vehicle=data.get("driver_vehicle"),
            driver_license_plate=data.get("driver_license_plate"),
            tracking_url=data.get("tracking_url"),
        )


def get_delivery_adapter(provider: DeliveryProvider) -> DeliveryProviderAdapter:
    """Get delivery provider adapter based on configuration."""
    if get_settings().delivery_provider_mode.upper() == "LIVE":
        return HttpDeliveryAdapter(provider)
    return MockDeliveryAdapter(provider)
