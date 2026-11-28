"""Regression tests for backend requirements from the final-project brief."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.api.controllers.notifications import NotificationController
from app.domain.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    PromotionType,
    RestaurantStatus,
)
from app.repositories.payment import PaymentRepository
from app.services.notifications import NotificationEvent, NotificationService, get_notification_hub
from app.services.order import OrderService
from tests.factories import (
    create_delivery,
    create_menu_item,
    create_order,
    create_payment,
    create_promotion,
    create_restaurant,
    create_user,
)
from tests.helpers import patch_order_repository_add


def _delivery_address(latitude: float = 10.0, longitude: float = 106.0) -> dict[str, object]:
    return {
        "street": "123 Main Street",
        "ward": "Ward 1",
        "district": "District 1",
        "city": "Ho Chi Minh City",
        "latitude": latitude,
        "longitude": longitude,
    }


def _order_payload(restaurant_id: uuid.UUID, item_id: uuid.UUID) -> dict[str, object]:
    return {
        "restaurant_id": str(restaurant_id),
        "items": [{"menu_item_id": str(item_id), "quantity": 1, "special_instructions": None}],
        "delivery_address": {
            "street": "123 Main Street",
            "ward": "Ward 1",
            "district": "District 1",
            "city": "Ho Chi Minh City",
            "latitude": "10.0",
            "longitude": "106.0",
        },
        "delivery_provider": "AHAMOVE",
    }


async def test_delivery_quote_endpoint_returns_distance_based_fee(client_factory, customer_user) -> None:
    """A quote is available before payment and reflects distance/provider."""
    payload = {
        "pickup": {
            "street": "Restaurant Street",
            "ward": "Ward 1",
            "district": "District 1",
            "city": "Ho Chi Minh City",
            "latitude": "10.0",
            "longitude": "106.0",
        },
        "dropoff": {
            "street": "Customer Street",
            "ward": "Ward 2",
            "district": "District 1",
            "city": "Ho Chi Minh City",
            "latitude": "10.0",
            "longitude": "106.1",
        },
        "provider": "LALAMOVE",
    }

    async with client_factory(customer_user) as client:
        response = await client.post("/v1/delivery/quote", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "LALAMOVE"
    assert Decimal(body["distance_km"]) > Decimal("10")
    assert Decimal(body["fee"]) > Decimal("15000")
    assert body["estimated_duration_minutes"] > 10


async def test_restaurant_busy_endpoint_sets_and_clears_window(client_factory, session, owner_user) -> None:
    """Owners can set and clear the allowed temporary busy windows."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)

    async with client_factory(owner_user) as client:
        busy = await client.patch(f"/v1/restaurant/{restaurant.id}/busy", json={"duration_minutes": 15})
        assert busy.status_code == 200
        assert busy.json()["busy_until"] is not None

        invalid = await client.patch(f"/v1/restaurant/{restaurant.id}/busy", json={"duration_minutes": 20})
        assert invalid.status_code == 422

        cleared = await client.patch(f"/v1/restaurant/{restaurant.id}/busy", json={"duration_minutes": None})
        assert cleared.status_code == 200
        assert cleared.json()["busy_until"] is None


async def test_restaurant_status_and_schedules_round_trip(client_factory, session, owner_user) -> None:
    """Status, weekly schedule, and custom schedule survive API updates."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    weekly_schedule = {"monday": {"open": "09:00", "close": "21:00"}}
    custom_schedules = [{"date": "2030-01-01", "closed": True}]

    async with client_factory(owner_user) as client:
        updated = await client.patch(
            f"/v1/restaurant/{restaurant.id}",
            json={
                "status": "CLOSED",
                "weekly_schedule": weekly_schedule,
                "custom_schedules": custom_schedules,
            },
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["status"] == "CLOSED"
        assert body["weekly_schedule"] == weekly_schedule
        assert body["custom_schedules"] == custom_schedules

        fetched = await client.get(f"/v1/restaurant/{restaurant.id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "CLOSED"
        assert fetched.json()["weekly_schedule"] == weekly_schedule


async def test_order_response_exposes_provider_and_allergens(
    client_factory, session, customer_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order responses expose provider choice and allergen snapshots."""
    patch_order_repository_add(monkeypatch)
    restaurant = await create_restaurant(session)
    item = await create_menu_item(session, restaurant_id=restaurant.id, allergens=["Peanut", "Shellfish"])
    payload = _order_payload(restaurant.id, item.id)
    payload["delivery_provider"] = "LALAMOVE"

    async with client_factory(customer_user) as client:
        response = await client.post("/v1/order/", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["delivery_provider"] == "LALAMOVE"
    assert body["items"][0]["allergens"] == ["Peanut", "Shellfish"]


async def test_order_creation_rejects_busy_and_closed_restaurants(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Busy and closed restaurants cannot accept new orders."""
    patch_order_repository_add(monkeypatch)
    customer = await create_user(session)
    busy = await create_restaurant(session, busy_until=datetime.utcnow() + timedelta(minutes=15))
    closed = await create_restaurant(session, status=RestaurantStatus.CLOSED)
    busy_item = await create_menu_item(session, restaurant_id=busy.id)
    closed_item = await create_menu_item(session, restaurant_id=closed.id)
    service = OrderService(session)

    with pytest.raises(ValueError, match="closed or busy"):
        await service.create_order(
            customer_id=customer.id,
            restaurant_id=busy.id,
            items=[{"menu_item_id": busy_item.id, "quantity": 1, "special_instructions": None, "modifier_ids": []}],
            delivery_address=_delivery_address(),  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="closed or busy"):
        await service.create_order(
            customer_id=customer.id,
            restaurant_id=closed.id,
            items=[{"menu_item_id": closed_item.id, "quantity": 1, "special_instructions": None, "modifier_ids": []}],
            delivery_address=_delivery_address(),  # type: ignore[arg-type]
        )


async def test_voucher_user_usage_limit_blocks_second_order(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """A customer can use a per-user voucher only once."""
    patch_order_repository_add(monkeypatch)
    customer = await create_user(session)
    restaurant = await create_restaurant(session)
    item = await create_menu_item(session, restaurant_id=restaurant.id, price=Decimal("100000"))
    await create_promotion(
        session,
        restaurant_id=restaurant.id,
        name="ONCE",
        type=PromotionType.FIXED_AMOUNT,
        value=Decimal("20000"),
        user_usage_limit=1,
    )
    service = OrderService(session)

    first = await service.create_order(
        customer_id=customer.id,
        restaurant_id=restaurant.id,
        items=[{"menu_item_id": item.id, "quantity": 1, "special_instructions": None, "modifier_ids": []}],
        delivery_address=_delivery_address(),  # type: ignore[arg-type]
        promotion_code="ONCE",
    )
    assert first.discount_amount.amount == Decimal("20000")

    with pytest.raises(ValueError, match="Promotion usage limit reached"):
        await service.create_order(
            customer_id=customer.id,
            restaurant_id=restaurant.id,
            items=[{"menu_item_id": item.id, "quantity": 1, "special_instructions": None, "modifier_ids": []}],
            delivery_address=_delivery_address(),  # type: ignore[arg-type]
            promotion_code="ONCE",
        )


async def test_cancelling_prepaid_order_refunds_payment(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejecting a prepaid order refunds the completed payment."""
    patch_order_repository_add(monkeypatch)
    order = await create_order(session, status=OrderStatus.CONFIRMED)
    payment = await create_payment(session, order_id=order.id, status=PaymentStatus.COMPLETED)

    cancelled = await OrderService(session).update_order_status(
        order.id, OrderStatus.CANCELLED, "Rejected by restaurant"
    )

    assert cancelled.status == OrderStatus.CANCELLED
    refreshed = await PaymentRepository(session).get(payment.id)
    assert refreshed is not None
    assert refreshed.status == PaymentStatus.REFUNDED


async def test_delivery_dispatch_returns_driver_and_quote_details(
    client_factory, session, owner_user, customer_user
) -> None:
    """Dispatch returns driver identity, vehicle, plate, and quote details."""
    order = await create_order(session, customer_id=customer_user.id, status=OrderStatus.READY_FOR_PICKUP)

    async with client_factory(owner_user) as client:
        response = await client.post(f"/v1/delivery/{order.id}/delivery/dispatch", json={"provider": "LALAMOVE"})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "LALAMOVE"
    assert body["driver_name"] == "Nguyễn Văn A (Mock)"
    assert body["driver_vehicle"] == "Xe máy"
    assert body["driver_license_plate"] == "51B-12345"
    assert Decimal(body["distance_km"]) >= Decimal("0")
    assert body["estimated_duration_minutes"] >= 10


async def test_notification_service_pushes_order_and_delivery_events(session, owner_user, customer_user) -> None:
    """New-order and delivery events reach both customer and owner."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    order = await create_order(session, customer_id=customer_user.id, restaurant_id=restaurant.id)
    delivery = await create_delivery(session, order_id=order.id)
    hub = get_notification_hub()
    customer_queue = hub.subscribe(customer_user.id)
    owner_queue = hub.subscribe(owner_user.id)

    try:
        service = NotificationService(session)
        await service.notify_order_created(order)
        customer_event = await customer_queue.get()
        owner_event = await owner_queue.get()
        assert customer_event.type == "ORDER_STATUS"
        assert owner_event.type == "NEW_ORDER"
        assert "cần xác nhận" in owner_event.message

        await service.notify_delivery_status(order, delivery, DeliveryStatus.DRIVER_ASSIGNED)
        customer_delivery = await customer_queue.get()
        owner_delivery = await owner_queue.get()
        assert customer_delivery.type == "DELIVERY_STATUS"
        assert owner_delivery.type == "DELIVERY_STATUS"
        assert "shipper" in customer_delivery.message
    finally:
        hub.unsubscribe(customer_user.id, customer_queue)
        hub.unsubscribe(owner_user.id, owner_queue)


async def test_notification_stream_emits_published_event(customer_user) -> None:
    """The SSE endpoint streams published notification events."""
    hub = get_notification_hub()
    response = await NotificationController().stream_notifications(customer_user)
    chunks = response.body_iterator

    try:
        # Get the first chunk (connection message)
        chunk_iterator = chunks.__aiter__()
        first_chunk = await chunk_iterator.__anext__()
        first_chunk_str = first_chunk.decode() if isinstance(first_chunk, bytes) else str(first_chunk)
        assert first_chunk_str == ": connected\n\n"

        await hub.publish(
            NotificationEvent(
                id=uuid.uuid4().hex,
                user_id=customer_user.id,
                order_id=uuid.uuid4(),
                type="ORDER_STATUS",
                status="CONFIRMED",
                message="Quán đang chuẩn bị món",
                created_at=datetime.utcnow(),
            )
        )

        # Get the next chunk (the actual event)
        second_chunk = await chunk_iterator.__anext__()
        second_chunk_str = second_chunk.decode() if isinstance(second_chunk, bytes) else str(second_chunk)
        assert second_chunk_str.startswith("event: ORDER_STATUS\ndata: ")
        data = second_chunk_str.split("data: ", 1)[1].strip()
        assert json.loads(data)["message"] == "Quán đang chuẩn bị món"
    finally:
        if hasattr(chunks, "aclose"):
            await chunks.aclose()
