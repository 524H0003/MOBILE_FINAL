"""Order service."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import MenuItem as DomainMenuItem
from app.domain.entities import Order as DomainOrder
from app.domain.entities import OrderItem as DomainOrderItem
from app.domain.entities import Restaurant as DomainRestaurant
from app.domain.enums import DeliveryProvider, OrderStatus, PaymentStatus, PromotionType
from app.domain.modifiers import Modifier as DomainModifier
from app.domain.value_objects import Address, Coordinates, Money
from app.orm.models import Modifier as ModifierModel
from app.repositories.menu_item import MenuItemRepository
from app.repositories.order import OrderRepository
from app.repositories.promotion import PromotionRepository
from app.repositories.restaurant import RestaurantRepository
from app.services.delivery import DeliveryService
from app.services.notifications import NotificationService
from app.services.payment import PaymentService


class OrderItemInput(TypedDict):
    """Input for an order item."""

    menu_item_id: uuid.UUID
    quantity: int
    special_instructions: str | None
    modifier_ids: list[uuid.UUID]


class AddressInput(TypedDict):
    """Input for a delivery address."""

    street: str
    ward: str
    district: str
    city: str
    latitude: float | None
    longitude: float | None


class OrderService:
    """Order service for order management."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._order_repo = OrderRepository(session)
        self._menu_item_repo = MenuItemRepository(session)
        self._promotion_repo = PromotionRepository(session)
        self._restaurant_repo = RestaurantRepository(session)
        self._delivery_service = DeliveryService(session)

    async def create_order(
        self,
        *,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        items: list[OrderItemInput],
        delivery_address: AddressInput,
        promotion_code: str | None = None,
        delivery_provider: DeliveryProvider = DeliveryProvider.AHAMOVE,
        notes: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> DomainOrder:
        """Create a new order with items and calculate totals."""
        restaurant = await self._validate_restaurant(restaurant_id)
        order_items, subtotal = await self._build_order_items(items)
        self._validate_minimum_order(subtotal, restaurant)

        delivery_fee, dropoff_coordinates = await self._calculate_delivery_fee(
            restaurant, delivery_address, delivery_provider
        )

        discount_amount, promotion_id = await self._apply_promotion(
            promotion_code, restaurant_id, customer_id, subtotal
        )

        order = self._create_order(
            customer_id=customer_id,
            restaurant_id=restaurant_id,
            items=order_items,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            discount_amount=discount_amount,
            promotion_id=promotion_id,
            promotion_code=promotion_code,
            delivery_provider=delivery_provider,
            delivery_address=delivery_address,
            delivery_coordinates=dropoff_coordinates,
            customer_note=notes,
        )

        saved_order = await self._save_order(order, promotion_id)
        await NotificationService(self._session).notify_order_created(saved_order)
        return saved_order

    async def _validate_restaurant(self, restaurant_id: uuid.UUID) -> DomainRestaurant:
        """Validate restaurant exists and is open for ordering."""
        restaurant = await self._restaurant_repo.get(restaurant_id)
        if not restaurant:
            raise ValueError("Restaurant not found")
        if not restaurant.is_active:
            raise ValueError("Restaurant is not active")
        if not restaurant.is_open_at():
            raise ValueError("Restaurant is closed or busy")
        return restaurant

    async def _build_order_items(self, items: list[OrderItemInput]) -> tuple[list[DomainOrderItem], Decimal]:
        """Build order items with modifiers and calculate subtotal."""
        order_items: list[DomainOrderItem] = []
        subtotal = Decimal("0")

        for item_data in items:
            menu_item = await self._menu_item_repo.get(item_data["menu_item_id"])
            if not menu_item:
                raise ValueError(f"Menu item {item_data['menu_item_id']} not found")
            if str(menu_item.status) != "AVAILABLE":
                raise ValueError(f"Menu item {menu_item.name} is not available")

            quantity = item_data["quantity"]
            special_instructions = item_data.get("special_instructions")
            modifier_ids = item_data.get("modifier_ids", [])

            modifiers = await self._load_modifiers(menu_item, modifier_ids)
            order_item = DomainOrderItem.create(
                order_id=uuid.uuid4(),
                menu_item_id=menu_item.id,
                menu_item_name=menu_item.name,
                menu_item_price=menu_item.price,
                quantity=quantity,
                note=special_instructions,
                modifiers=modifiers,
                allergens=menu_item.allergens,
            )
            order_items.append(order_item)
            subtotal += order_item.subtotal.amount

        return order_items, subtotal

    async def _load_modifiers(self, menu_item: DomainMenuItem, modifier_ids: list[uuid.UUID]) -> list[DomainModifier]:
        """Load and validate modifiers for a menu item."""
        if not modifier_ids:
            return []

        # Get all modifiers for this menu item's modifier groups
        all_modifier_ids = set()
        for group in menu_item.modifier_groups:
            for mod in group.modifiers:
                all_modifier_ids.add(mod.id)

        # Check that all selected modifiers belong to this menu item
        invalid_modifiers = [mid for mid in modifier_ids if mid not in all_modifier_ids]
        if invalid_modifiers:
            raise ValueError(f"Invalid modifier IDs for menu item {menu_item.name}: {invalid_modifiers}")

        # Validate selection constraints per modifier group
        for group in menu_item.modifier_groups:
            group_modifier_ids = [mid for mid in modifier_ids if any(m.id == mid for m in group.modifiers)]
            selected_count = len(group_modifier_ids)
            if selected_count < group.min_selection:
                raise ValueError(f"Modifier group '{group.name}' requires at least {group.min_selection} selection(s)")
            if selected_count > group.max_selection:
                raise ValueError(f"Modifier group '{group.name}' allows at most {group.max_selection} selection(s)")

        # Load the modifier domain entities
        modifiers: list[DomainModifier] = []
        for mod_id in modifier_ids:
            stmt = select(ModifierModel).where(ModifierModel.id == mod_id)
            result = await self._session.execute(stmt)
            mod_model = result.scalar_one_or_none()
            if mod_model:
                modifiers.append(
                    DomainModifier(
                        id=mod_model.id,
                        created_at=mod_model.created_at,
                        updated_at=mod_model.updated_at,
                        group_id=mod_model.group_id,
                        name=mod_model.name,
                        price=Money(amount=mod_model.price),
                    )
                )
        return modifiers

    def _validate_minimum_order(self, subtotal: Decimal, restaurant: DomainRestaurant) -> None:
        """Validate minimum order amount."""
        if subtotal < restaurant.min_order_amount.amount:
            raise ValueError(f"Minimum order amount is {restaurant.min_order_amount}")

    async def _calculate_delivery_fee(
        self,
        restaurant: DomainRestaurant,
        delivery_address: AddressInput,
        delivery_provider: DeliveryProvider,
    ) -> tuple[Decimal, Coordinates | None]:
        """Calculate delivery fee and dropoff coordinates."""
        pickup_coordinates = (
            Coordinates(
                latitude=restaurant.address.latitude,
                longitude=restaurant.address.longitude,
            )
            if restaurant.address.latitude is not None and restaurant.address.longitude is not None
            else None
        )
        dropoff_coordinates = (
            Coordinates(
                latitude=delivery_address["latitude"] or 0.0,
                longitude=delivery_address["longitude"] or 0.0,
            )
            if delivery_address.get("latitude") is not None and delivery_address.get("longitude") is not None
            else None
        )
        quote = await self._delivery_service.get_quote(
            pickup=restaurant.address,
            dropoff=Address(**delivery_address),
            provider=delivery_provider,
            pickup_coordinates=pickup_coordinates,
            dropoff_coordinates=dropoff_coordinates,
        )
        quote_fee = Decimal(str(quote["fee"]))
        delivery_fee = max(restaurant.delivery_fee.amount, quote_fee)
        return delivery_fee, dropoff_coordinates

    async def _apply_promotion(
        self,
        promotion_code: str | None,
        restaurant_id: uuid.UUID,
        customer_id: uuid.UUID,
        subtotal: Decimal,
    ) -> tuple[Decimal, uuid.UUID | None]:
        """Apply promotion if provided and return discount amount and promotion ID."""
        discount_amount = Decimal("0")
        promotion_id = None

        if not promotion_code:
            return discount_amount, promotion_id

        promotions = await self._promotion_repo.get_active_by_restaurant(restaurant_id)
        promo = next((p for p in promotions if p.name == promotion_code), None)
        if not promo or not promo.is_valid():
            return discount_amount, promotion_id

        if promo.min_order_amount and subtotal < promo.min_order_amount.amount:
            raise ValueError(f"Minimum order amount for this promotion is {promo.min_order_amount}")
        used = await self._order_repo.count_by_customer_and_promotion(customer_id, promo.id)
        if used >= promo.user_usage_limit:
            raise ValueError("Promotion usage limit reached for this customer")

        if promo.type == PromotionType.PERCENTAGE:
            discount_amount = subtotal * (promo.value / Decimal("100"))
            if promo.max_discount_amount:
                discount_amount = min(discount_amount, promo.max_discount_amount.amount)
        elif promo.type == PromotionType.FIXED_AMOUNT:
            discount_amount = min(promo.value, subtotal)
        # FREE_DELIVERY is intentionally not applied to shipping.
        promotion_id = promo.id

        # Ensure discount doesn't exceed subtotal and money has at most 2 decimals.
        discount_amount = min(discount_amount, subtotal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return discount_amount, promotion_id

    def _create_order(
        self,
        *,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        items: list[DomainOrderItem],
        subtotal: Decimal,
        delivery_fee: Decimal,
        discount_amount: Decimal,
        promotion_id: uuid.UUID | None,
        promotion_code: str | None,
        delivery_provider: DeliveryProvider,
        delivery_address: AddressInput,
        delivery_coordinates: Coordinates | None,
        customer_note: str | None,
    ) -> DomainOrder:
        """Create order domain entity."""
        total_amount = subtotal + delivery_fee - discount_amount
        order = DomainOrder(
            customer_id=customer_id,
            restaurant_id=restaurant_id,
            items=items,
            subtotal=Money(amount=subtotal),
            delivery_fee=Money(amount=delivery_fee),
            discount_amount=Money(amount=discount_amount),
            total_amount=Money(amount=total_amount),
            promotion_id=promotion_id,
            promotion_code=promotion_code,
            delivery_provider=delivery_provider,
            delivery_address=Address(**delivery_address),
            delivery_coordinates=delivery_coordinates,
            customer_note=customer_note,
        )
        # Set order_id on items
        for item in order.items:
            item.order_id = order.id
        return order

    async def _save_order(self, order: DomainOrder, promotion_id: uuid.UUID | None) -> DomainOrder:
        """Save order and update promotion usage count."""
        saved_order = await self._order_repo.add(order)

        if promotion_id:
            promo = await self._promotion_repo.get(promotion_id)
            if promo:
                promo.usage_count += 1
                await self._promotion_repo.update(promo)

        return saved_order

    async def get_order(self, order_id: uuid.UUID) -> DomainOrder | None:
        """Get order by ID."""
        return await self._order_repo.get(order_id)

    async def get_customer_orders(
        self, customer_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[DomainOrder]:
        """Get orders for a customer."""
        return await self._order_repo.get_by_customer(customer_id, limit=limit, offset=offset)

    async def get_restaurant_orders(
        self, restaurant_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[DomainOrder]:
        """Get orders for a restaurant."""
        return await self._order_repo.get_by_restaurant(restaurant_id, limit=limit, offset=offset)

    async def get_orders_by_status(
        self, status: OrderStatus, *, limit: int = 100, offset: int = 0
    ) -> list[DomainOrder]:
        """Get orders by status."""
        return await self._order_repo.get_by_status(status, limit=limit, offset=offset)

    async def list_all_orders(self, *, limit: int = 100, offset: int = 0) -> list[DomainOrder]:
        """List all orders (staff use)."""
        return await self._order_repo.list_all(limit=limit, offset=offset)

    async def update_order_status(
        self, order_id: uuid.UUID, new_status: OrderStatus, reason: str | None = None
    ) -> DomainOrder:
        """Update order status."""
        order = await self._order_repo.get(order_id)
        if not order:
            raise ValueError("Order not found")

        if not order.can_transition_to(new_status):
            raise ValueError(f"Cannot transition from {order.status} to {new_status}")

        order.transition_to(new_status)
        if new_status == OrderStatus.CANCELLED and reason:
            order.cancellation_reason = reason

        updated_order = await self._order_repo.update(order)

        # Refund prepaid orders when the restaurant rejects/cancels.
        if new_status == OrderStatus.CANCELLED:
            payment = await PaymentService(self._session).get_payment_by_order(order_id)
            if payment is not None and PaymentStatus(payment.status) == PaymentStatus.COMPLETED:
                await PaymentService(self._session).refund_payment(payment.id)

        await NotificationService(self._session).notify_order_status(updated_order, new_status)

        # If order is ready for pickup, auto-dispatch delivery
        if new_status == OrderStatus.READY_FOR_PICKUP:
            await self._delivery_service.auto_dispatch(order_id)

        return updated_order

    async def cancel_order(self, order_id: uuid.UUID, reason: str) -> DomainOrder:
        """Cancel an order."""
        return await self.update_order_status(order_id, OrderStatus.CANCELLED, reason)

    async def reorder(self, order_id: uuid.UUID, customer_id: uuid.UUID) -> DomainOrder:
        """Create a new order based on a previous order."""
        original_order = await self._order_repo.get(order_id)
        if not original_order:
            raise ValueError("Original order not found")
        if original_order.customer_id != customer_id:
            raise ValueError("Order does not belong to this customer")
        if original_order.status != OrderStatus.DELIVERED:
            raise ValueError("Can only reorder delivered orders")

        items: list[OrderItemInput] = [
            {
                "menu_item_id": item.menu_item_id,
                "quantity": item.quantity,
                "special_instructions": item.note,
                "modifier_ids": [mod.id for mod in item.modifiers],
            }
            for item in original_order.items
        ]

        return await self.create_order(
            customer_id=customer_id,
            restaurant_id=original_order.restaurant_id,
            items=items,
            delivery_address={
                "street": original_order.delivery_address.street,
                "ward": original_order.delivery_address.ward,
                "district": original_order.delivery_address.district,
                "city": original_order.delivery_address.city,
                "latitude": original_order.delivery_address.latitude,
                "longitude": original_order.delivery_address.longitude,
            },
            notes=original_order.customer_note,
        )
