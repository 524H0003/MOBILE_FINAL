"""Notification service and in-process event hub for real-time updates."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.notification import get_notification_adapter
from app.domain.entities import Delivery, Order
from app.domain.enums import DeliveryStatus, OrderStatus
from app.repositories.restaurant import RestaurantRepository


class NotificationEvent(BaseModel):
    """Event pushed to connected clients over SSE."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: uuid.UUID
    order_id: uuid.UUID
    type: str
    status: str
    message: str
    created_at: datetime


class NotificationHub:
    """Fan-out hub for server-sent notification events."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[NotificationEvent]]] = defaultdict(set)

    def subscribe(self, user_id: uuid.UUID) -> asyncio.Queue[NotificationEvent]:
        """Register a new subscriber queue."""
        queue: asyncio.Queue[NotificationEvent] = asyncio.Queue()
        self._subscribers[user_id].add(queue)
        return queue

    def unsubscribe(self, user_id: uuid.UUID, queue: asyncio.Queue[NotificationEvent]) -> None:
        """Remove a subscriber queue."""
        queues = self._subscribers.get(user_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(user_id, None)

    async def publish(self, event: NotificationEvent) -> None:
        """Publish an event to all queues of the target user."""
        for queue in list(self._subscribers.get(event.user_id, set())):
            await queue.put(event)


notification_hub = NotificationHub()


def get_notification_hub() -> NotificationHub:
    """Return the process-wide notification hub."""
    return notification_hub


class NotificationService:
    """Send notifications through the configured adapter and local hub."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._adapter = get_notification_adapter()
        self._restaurant_repo = RestaurantRepository(session)

    async def _publish(
        self,
        *,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        event_type: str,
        status: str,
        message: str,
    ) -> None:
        await get_notification_hub().publish(
            NotificationEvent(
                id=uuid.uuid4().hex,
                user_id=user_id,
                order_id=order_id,
                type=event_type,
                status=status,
                message=message,
                created_at=datetime.utcnow(),
            )
        )

    async def notify_order_created(self, order: Order) -> None:
        """Notify the customer and ring the owner's new-order bell."""
        status = OrderStatus(order.status)
        await self._adapter.send_order_status(user_id=order.customer_id, order_id=order.id, status=status)
        await self._publish(
            user_id=order.customer_id,
            order_id=order.id,
            event_type="ORDER_STATUS",
            status=status.value,
            message="Đơn hàng đã được gửi đến quán",
        )

        restaurant = await self._restaurant_repo.get(order.restaurant_id)
        if restaurant is not None:
            await self._adapter.send_order_status(user_id=restaurant.owner_id, order_id=order.id, status=status)
            await self._publish(
                user_id=restaurant.owner_id,
                order_id=order.id,
                event_type="NEW_ORDER",
                status=status.value,
                message=f"Đơn hàng mới #{str(order.id)[:8]} cần xác nhận",
            )

    async def notify_order_status(self, order: Order, status: OrderStatus) -> None:
        """Push order status changes to the customer."""
        normalized = OrderStatus(status)
        await self._adapter.send_order_status(user_id=order.customer_id, order_id=order.id, status=normalized)
        message = self._order_message(normalized)
        await self._publish(
            user_id=order.customer_id,
            order_id=order.id,
            event_type="ORDER_STATUS",
            status=normalized.value,
            message=message,
        )

    async def notify_delivery_status(self, order: Order, delivery: Delivery, status: DeliveryStatus) -> None:
        """Push delivery status changes to customer and restaurant owner."""
        normalized = DeliveryStatus(status)
        await self._adapter.send_delivery_status(user_id=order.customer_id, order_id=order.id, status=normalized)
        message = self._delivery_message(normalized, delivery)
        await self._publish(
            user_id=order.customer_id,
            order_id=order.id,
            event_type="DELIVERY_STATUS",
            status=normalized.value,
            message=message,
        )

        restaurant = await self._restaurant_repo.get(order.restaurant_id)
        if restaurant is not None:
            await self._adapter.send_delivery_status(user_id=restaurant.owner_id, order_id=order.id, status=normalized)
            await self._publish(
                user_id=restaurant.owner_id,
                order_id=order.id,
                event_type="DELIVERY_STATUS",
                status=normalized.value,
                message=message,
            )

    @staticmethod
    def _order_message(status: OrderStatus) -> str:
        messages: dict[OrderStatus, str] = {
            OrderStatus.CONFIRMED: "Quán đang chuẩn bị món",
            OrderStatus.PREPARING: "Quán đang chuẩn bị món",
            OrderStatus.READY_FOR_PICKUP: "Món đã sẵn sàng, đang gọi shipper",
            OrderStatus.DISPATCHED: "Shipper đã nhận đơn",
            OrderStatus.DELIVERING: "Shipper đang giao hàng",
            OrderStatus.DELIVERED: "Đơn hàng đã giao thành công",
            OrderStatus.CANCELLED: "Đơn hàng đã bị hủy",
            OrderStatus.REFUNDED: "Đơn hàng đã được hoàn tiền",
        }
        return messages.get(status, f"Trạng thái đơn hàng: {status.value}")

    @staticmethod
    def _delivery_message(status: DeliveryStatus, delivery: Delivery) -> str:
        driver = delivery.driver_name or "shipper"
        messages: dict[DeliveryStatus, str] = {
            DeliveryStatus.DRIVER_ASSIGNED: f"{driver} đã nhận giao đơn",
            DeliveryStatus.DRIVER_EN_ROUTE_TO_PICKUP: f"{driver} đang đến lấy món",
            DeliveryStatus.PICKED_UP: f"{driver} đã lấy món",
            DeliveryStatus.IN_TRANSIT: f"{driver} đang giao hàng",
            DeliveryStatus.DELIVERED: "Đơn hàng đã giao thành công",
            DeliveryStatus.FAILED: "Giao hàng thất bại",
            DeliveryStatus.CANCELLED: "Giao hàng đã bị hủy",
        }
        return messages.get(status, f"Trạng thái giao hàng: {status.value}")
