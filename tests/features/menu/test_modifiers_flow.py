"""Integration tests for menu item modifier groups and order modifiers."""

from __future__ import annotations

import uuid
from decimal import Decimal

from tests.factories import create_category, create_menu_item, create_restaurant, create_user
from tests.helpers import assert_error, assert_page, patch_order_repository_add


def _menu_item_with_modifiers_payload(category_id: uuid.UUID) -> dict[str, object]:
    return {
        "category_id": str(category_id),
        "name": "Grilled Chicken Rice",
        "description": "Delicious grilled chicken with rice",
        "price": "85000",
        "preparation_time_minutes": 20,
        "modifier_groups": [
            {
                "name": "Size (Required - Choose 1)",
                "min_selection": 1,
                "max_selection": 1,
                "display_order": 1,
                "modifiers": [
                    {"name": "Small", "price": "0"},
                    {"name": "Large", "price": "15000"},
                ],
            },
            {
                "name": "Add-ons (Optional - Choose multiple)",
                "min_selection": 0,
                "max_selection": 3,
                "display_order": 2,
                "modifiers": [
                    {"name": "Extra Chicken", "price": "20000"},
                    {"name": "Extra Rice", "price": "10000"},
                    {"name": "Fried Egg", "price": "8000"},
                ],
            },
        ],
    }


def _order_payload(
    restaurant_id: uuid.UUID,
    item_id: uuid.UUID,
    modifier_ids: list[str],
) -> dict[str, object]:
    return {
        "restaurant_id": str(restaurant_id),
        "items": [
            {
                "menu_item_id": str(item_id),
                "quantity": 1,
                "special_instructions": "No chili",
                "modifier_ids": modifier_ids,
            }
        ],
        "delivery_address": {
            "street": "456 Customer St",
            "ward": "Ward 2",
            "district": "District 1",
            "city": "Ho Chi Minh City",
            "latitude": "10.0",
            "longitude": "106.0",
        },
    }


async def test_create_menu_item_with_modifier_groups(client_factory, session, owner_user) -> None:
    """Owner can create a menu item with modifier groups."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["name"] == "Grilled Chicken Rice"
        groups = body["modifier_groups"]
        assert len(groups) == 2
        assert groups[0]["name"] == "Size (Required - Choose 1)"
        assert groups[0]["min_selection"] == 1
        assert groups[0]["max_selection"] == 1
        assert len(groups[0]["modifiers"]) == 2
        assert groups[1]["name"] == "Add-ons (Optional - Choose multiple)"
        assert groups[1]["min_selection"] == 0
        assert groups[1]["max_selection"] == 3
        assert len(groups[1]["modifiers"]) == 3

        # Listed item should carry modifier groups too
        listed = await client.get(f"/v1/menu/{restaurant.id}/items")
        assert listed.status_code == 200
        items = assert_page(listed.json())
        assert len(items) == 1
        assert len(items[0]["modifier_groups"]) == 2


async def test_order_with_valid_modifiers(client_factory, session, owner_user, customer_user, monkeypatch) -> None:
    """Customer can order with valid modifier selections; total includes modifiers."""
    patch_order_repository_add(monkeypatch)
    restaurant = await create_restaurant(session, owner_id=owner_user.id, delivery_fee=Decimal("30000"))
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        assert created.status_code == 200, created.text
        item = created.json()

    modifier_ids: dict[str, str] = {}
    for group in item["modifier_groups"]:
        for mod in group["modifiers"]:
            modifier_ids[mod["name"]] = mod["id"]

    async with client_factory(customer_user) as client:
        resp = await client.post(
            "/v1/order/",
            json=_order_payload(
                restaurant.id,
                uuid.UUID(item["id"]),
                [modifier_ids["Large"], modifier_ids["Extra Chicken"], modifier_ids["Fried Egg"]],
            ),
        )
        assert resp.status_code == 200, resp.text
        order = resp.json()
        # 85000 base + 15000 (Large) + 20000 (Extra Chicken) + 8000 (Fried Egg) = 128000
        assert Decimal(order["subtotal"]) == Decimal("128000")
        assert Decimal(order["delivery_fee"]) == Decimal("30000")
        assert Decimal(order["total_amount"]) == Decimal("158000")
        mods = order["items"][0]["modifiers"]
        assert {m["name"] for m in mods} == {"Large", "Extra Chicken", "Fried Egg"}


async def test_order_missing_required_modifier(client_factory, session, owner_user, customer_user) -> None:
    """Order fails when a required modifier group has too few selections."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        item = created.json()

    modifier_ids: dict[str, str] = {}
    for group in item["modifier_groups"]:
        for mod in group["modifiers"]:
            modifier_ids[mod["name"]] = mod["id"]

    async with client_factory(customer_user) as client:
        # Only add-on selected, required Size group has 0
        resp = await client.post(
            "/v1/order/",
            json=_order_payload(restaurant.id, uuid.UUID(item["id"]), [modifier_ids["Extra Chicken"]]),
        )
        assert_error(resp, 400, "Modifier group 'Size (Required - Choose 1)' requires at least 1 selection(s)")


async def test_order_too_many_modifiers_in_group(client_factory, session, owner_user, customer_user) -> None:
    """Order fails when a modifier group exceeds max_selection."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        item = created.json()

    modifier_ids: dict[str, str] = {}
    for group in item["modifier_groups"]:
        for mod in group["modifiers"]:
            modifier_ids[mod["name"]] = mod["id"]

    async with client_factory(customer_user) as client:
        # Both Small and Large selected, but max_selection is 1
        resp = await client.post(
            "/v1/order/",
            json=_order_payload(
                restaurant.id,
                uuid.UUID(item["id"]),
                [modifier_ids["Small"], modifier_ids["Large"]],
            ),
        )
        assert_error(resp, 400, "Modifier group 'Size (Required - Choose 1)' allows at most 1 selection(s)")


async def test_order_invalid_modifier_id(client_factory, session, owner_user, customer_user) -> None:
    """Order fails when a modifier ID does not belong to the menu item."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        item = created.json()

    size_id = next(
        mod["id"]
        for group in item["modifier_groups"]
        for mod in group["modifiers"]
        if mod["name"] == "Large"
    )

    async with client_factory(customer_user) as client:
        resp = await client.post(
            "/v1/order/",
            json=_order_payload(
                restaurant.id,
                uuid.UUID(item["id"]),
                [size_id, str(uuid.uuid4())],
            ),
        )
        assert resp.status_code == 400
        assert "Invalid modifier IDs" in resp.json()["detail"]


async def test_reorder_preserves_modifiers(
    client_factory, session, owner_user, customer_user, monkeypatch
) -> None:
    """Reorder carries modifiers from the original delivered order."""
    patch_order_repository_add(monkeypatch)
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        item = created.json()

    modifier_ids: dict[str, str] = {}
    for group in item["modifier_groups"]:
        for mod in group["modifiers"]:
            modifier_ids[mod["name"]] = mod["id"]

    async with client_factory(customer_user) as client:
        order_resp = await client.post(
            "/v1/order/",
            json=_order_payload(
                restaurant.id,
                uuid.UUID(item["id"]),
                [modifier_ids["Large"], modifier_ids["Fried Egg"]],
            ),
        )
        assert order_resp.status_code == 200, order_resp.text
        order_id = order_resp.json()["id"]

    # Mark delivered directly in DB
    from app.domain.enums import OrderStatus
    from app.repositories.order import OrderRepository

    repo = OrderRepository(session)
    order = await repo.get(uuid.UUID(order_id))
    assert order is not None
    order.status = OrderStatus.DELIVERED
    await repo.update(order)

    async with client_factory(customer_user) as client:
        resp = await client.post(f"/v1/order/{order_id}/reorder")
        assert resp.status_code == 200, resp.text
        new_order = resp.json()
        assert new_order["id"] != order_id
        mods = new_order["items"][0]["modifiers"]
        assert {m["name"] for m in mods} == {"Large", "Fried Egg"}


async def test_update_menu_item_modifier_groups(client_factory, session, owner_user) -> None:
    """Owner can replace modifier groups via PATCH."""
    restaurant = await create_restaurant(session, owner_id=owner_user.id)
    category = await create_category(session, restaurant_id=restaurant.id)

    async with client_factory(owner_user) as client:
        created = await client.post(
            f"/v1/menu/{restaurant.id}/items",
            json=_menu_item_with_modifiers_payload(category.id),
        )
        item = created.json()

        updated = await client.patch(
            f"/v1/menu/items/{item['id']}",
            json={
                "modifier_groups": [
                    {
                        "name": "Sauce",
                        "min_selection": 0,
                        "max_selection": 1,
                        "display_order": 1,
                        "modifiers": [{"name": "Garlic", "price": "5000"}],
                    }
                ]
            },
        )
        assert updated.status_code == 200, updated.text
        groups = updated.json()["modifier_groups"]
        assert len(groups) == 1
        assert groups[0]["name"] == "Sauce"
        assert groups[0]["modifiers"][0]["name"] == "Garlic"