from __future__ import annotations

import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

_auth_mod = types.ModuleType("core.auth")
_auth_mod.get_current_user_id = MagicMock()
_auth_mod.get_current_access_token = MagicMock()
sys.modules["core.auth"] = _auth_mod

from fastapi import FastAPI, HTTPException
import httpx
import pytest

import api.routers.purchases as _purchases_module
from api.routers.purchases import router as _purchases_router

_DEP_GET_USER_ID = _purchases_module.get_current_user_id
_DEP_GET_ACCESS_TOKEN = _purchases_module.get_current_access_token

_test_app = FastAPI()
_test_app.include_router(_purchases_router, prefix="/purchases")


def _deps(user_id: str = "user-1") -> dict:
    return {
        _DEP_GET_USER_ID: lambda: user_id,
        _DEP_GET_ACCESS_TOKEN: lambda: "test-access-token",
    }


@pytest.mark.asyncio
async def test_purchase_item_403_non_member():
    _test_app.dependency_overrides = _deps()
    transport = httpx.ASGITransport(app=_test_app)
    with patch.object(_purchases_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_purchases_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member"), create=True):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/purchases/items/item-1",
                json={"list_id": "list-1"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_undo_purchase_403_non_member():
    _test_app.dependency_overrides = _deps()
    transport = httpx.ASGITransport(app=_test_app)
    with patch.object(_purchases_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_purchases_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member"), create=True):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete(
                "/purchases/items/item-1",
                params={"list_id": "list-1"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_purchase_item_scales_totals_by_quantity():
    _test_app.dependency_overrides = _deps("user-1")
    transport = httpx.ASGITransport(app=_test_app)

    shopping_lists_table = MagicMock()
    offers_table = MagicMock()
    purchase_history_table = MagicMock()

    shopping_lists_table.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
        data={
            "items": [
                {
                    "id": "item-1",
                    "name": "Latte",
                    "brand": "Granarolo",
                    "quantity": 3,
                    "image_url": "https://example.com/latte.png",
                    "category": "bevande",
                    "subcategory": "Acqua e Bibite",
                    "pinned_offer_id": "offer-1",
                    "found_deals": [
                        {
                            "format_label": "1 L",
                            "unit_price": "1,20 €/l",
                            "unit_price_value": 1.2,
                            "unit_price_unit": "l",
                            "unit_price_label": "1,20 €/l",
                        }
                    ],
                }
            ]
        }
    )
    offers_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{
            "id": "offer-1",
            "product_id": "prod-1",
            "price_offer": 1.2,
            "price_original": 1.6,
            "discount_pct": 25,
            "format_label": "1 L",
            "unit_price": "1,20 €/l",
            "unit_price_value": 1.2,
            "unit_price_unit": "l",
            "supermarket_id": "store-1",
            "supermarkets": {"name": "Esselunga"},
            "products": {
                "brand": "Granarolo",
                "image_url": "https://example.com/latte.png",
                "category": "bevande",
                "subcategory": "Acqua e Bibite",
            },
        }]
    )
    purchase_history_table.insert.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "purchase-1",
                "list_id": "list-1",
                "list_item_id": "item-1",
                "item_name": "Latte",
                "brand": "Granarolo",
                "format_label": "1 L",
                "image_url": "https://example.com/latte.png",
                "category": "bevande",
                "subcategory": "Acqua e Bibite",
                "product_id": "prod-1",
                "offer_id": "offer-1",
                "supermarket_id": "store-1",
                "supermarket_name": "Esselunga",
                "quantity": 3,
                "price_paid": 3.6,
                "price_original": 4.8,
                "discount_pct": 25,
                "unit_price": "1,20 €/l",
                "unit_price_value": 1.2,
                "unit_price_unit": "l",
                "unit_price_label": "1,20 €/l",
                "savings": 1.2,
                "purchased_at": "2026-05-08T10:00:00+00:00",
            }
        ]
    )

    sb = MagicMock()
    sb.table.side_effect = lambda name: {
        "shopping_lists": shopping_lists_table,
        "offers": offers_table,
        "purchase_history": purchase_history_table,
    }[name]

    rpc_mock = AsyncMock(return_value=None)
    with patch.object(_purchases_module, "get_supabase", return_value=sb), \
         patch.object(_purchases_module, "_verify_member", return_value=None), \
         patch.object(_purchases_module, "_rpc_update_list_item", new=rpc_mock), \
         patch.object(_purchases_module, "publish_list_sync_event") as publish_mock:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/purchases/items/item-1",
                json={"list_id": "list-1"},
            )

    assert resp.status_code == 201
    insert_payload = purchase_history_table.insert.call_args.args[0]
    assert insert_payload["quantity"] == 3
    assert insert_payload["price_paid"] == 3.6
    assert insert_payload["price_original"] == 4.8
    assert insert_payload["brand"] == "Granarolo"
    assert insert_payload["format_label"] == "1 L"
    assert insert_payload["image_url"] == "https://example.com/latte.png"
    assert insert_payload["unit_price_label"] == "1,20 €/l"
    assert resp.json()["quantity"] == 3.0
    assert resp.json()["price_paid"] == 3.6
    assert resp.json()["savings"] == 1.2
    assert resp.json()["brand"] == "Granarolo"
    assert resp.json()["format_label"] == "1 L"
    rpc_mock.assert_awaited_once()
    publish_mock.assert_called_once_with("list-1", "list_updated", "item_purchased")
    assert rpc_mock.await_args.args == (
        "list-1",
        "item-1",
        {
            "purchased": True,
            "purchased_by": "user-1",
            "purchased_at": rpc_mock.await_args.args[2]["purchased_at"],
        },
        "user-1",
        "test-access-token",
    )
    shopping_lists_table.update.assert_not_called()


@pytest.mark.asyncio
async def test_purchase_item_falls_back_when_pinned_offer_missing():
    _test_app.dependency_overrides = _deps("user-1")
    transport = httpx.ASGITransport(app=_test_app)

    shopping_lists_table = MagicMock()
    offers_table = MagicMock()
    purchase_history_table = MagicMock()

    shopping_lists_table.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
        data={
            "items": [
                {
                    "id": "item-1",
                    "name": "Latte",
                    "brand": "Granarolo",
                    "quantity": 1,
                    "image_url": "https://example.com/latte.png",
                    "category": "bevande",
                    "subcategory": "Acqua e Bibite",
                    "pinned_offer_id": "offer-missing",
                    "found_deals": [
                        {
                            "offer_id": "offer-fallback",
                            "product_id": "prod-1",
                            "supermarket_id": "store-1",
                            "supermarket_name": "Esselunga",
                            "price_offer": 1.2,
                            "price_original": 1.6,
                            "discount_pct": 25,
                            "format_label": "1 L",
                            "unit_price": "1,20 €/l",
                            "unit_price_value": 1.2,
                            "unit_price_unit": "l",
                            "unit_price_label": "1,20 €/l",
                        }
                    ],
                }
            ]
        }
    )
    offers_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[]
    )
    purchase_history_table.insert.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "purchase-1",
                "list_id": "list-1",
                "list_item_id": "item-1",
                "item_name": "Latte",
                "brand": "Granarolo",
                "format_label": "1 L",
                "image_url": "https://example.com/latte.png",
                "category": "bevande",
                "subcategory": "Acqua e Bibite",
                "product_id": "prod-1",
                "offer_id": None,
                "supermarket_id": "store-1",
                "supermarket_name": "Esselunga",
                "quantity": 1,
                "price_paid": 1.2,
                "price_original": 1.6,
                "discount_pct": 25,
                "unit_price": "1,20 €/l",
                "unit_price_value": 1.2,
                "unit_price_unit": "l",
                "unit_price_label": "1,20 €/l",
                "savings": 0.4,
                "purchased_at": "2026-05-08T10:00:00+00:00",
            }
        ]
    )

    sb = MagicMock()
    sb.table.side_effect = lambda name: {
        "shopping_lists": shopping_lists_table,
        "offers": offers_table,
        "purchase_history": purchase_history_table,
    }[name]

    rpc_mock = AsyncMock(return_value=None)
    with patch.object(_purchases_module, "get_supabase", return_value=sb), \
         patch.object(_purchases_module, "_verify_member", return_value=None), \
         patch.object(_purchases_module, "_rpc_update_list_item", new=rpc_mock), \
         patch.object(_purchases_module, "publish_list_sync_event") as publish_mock:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.post(
                "/purchases/items/item-1",
                json={"list_id": "list-1"},
            )

    assert resp.status_code == 201
    insert_payload = purchase_history_table.insert.call_args.args[0]
    assert insert_payload["offer_id"] is None
    assert insert_payload["supermarket_name"] == "Esselunga"
    assert insert_payload["price_paid"] == 1.2
    rpc_mock.assert_awaited_once()
    publish_mock.assert_called_once_with("list-1", "list_updated", "item_purchased")


@pytest.mark.asyncio
async def test_undo_purchase_uses_item_patch_rpc():
    _test_app.dependency_overrides = _deps("user-1")
    transport = httpx.ASGITransport(app=_test_app)

    shopping_lists_table = MagicMock()
    purchase_history_table = MagicMock()

    shopping_lists_table.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
        data={
            "items": [
                {
                    "id": "item-1",
                    "name": "Latte",
                    "purchased": True,
                    "purchased_by": "user-1",
                    "purchased_at": "2026-05-08T10:00:00+00:00",
                }
            ]
        }
    )
    purchase_history_table.delete.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )

    sb = MagicMock()
    sb.table.side_effect = lambda name: {
        "shopping_lists": shopping_lists_table,
        "purchase_history": purchase_history_table,
    }[name]

    rpc_mock = AsyncMock(return_value=None)
    with patch.object(_purchases_module, "get_supabase", return_value=sb), \
         patch.object(_purchases_module, "_verify_member", return_value=None), \
         patch.object(_purchases_module, "_rpc_update_list_item", new=rpc_mock), \
         patch.object(_purchases_module, "publish_list_sync_event") as publish_mock:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/purchases/items/item-1",
                params={"list_id": "list-1"},
            )

    assert resp.status_code == 204
    rpc_mock.assert_awaited_once_with(
        "list-1",
        "item-1",
        {
            "purchased": False,
            "purchased_by": None,
            "purchased_at": None,
        },
        "user-1",
        "test-access-token",
    )
    publish_mock.assert_called_once_with(
        "list-1",
        "list_updated",
        "item_purchase_undone",
    )
    purchase_history_table.delete.assert_called_once()
    shopping_lists_table.update.assert_not_called()


@pytest.mark.asyncio
async def test_get_history_uses_concrete_utc_cutoff():
    _test_app.dependency_overrides = _deps()
    transport = httpx.ASGITransport(app=_test_app)

    sb = MagicMock()
    records_table = MagicMock()
    summary_table = MagicMock()
    sb.table.side_effect = [records_table, summary_table]

    records_select = records_table.select.return_value
    records_eq = records_select.eq.return_value
    records_gte = records_eq.gte.return_value
    records_order = records_gte.order.return_value
    records_order.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

    summary_select = summary_table.select.return_value
    summary_eq = summary_select.eq.return_value
    summary_eq.gte.return_value.execute.return_value = MagicMock(data=[])

    with patch.object(_purchases_module, "get_supabase", return_value=sb):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/purchases/history", params={"days": 30})

    assert resp.status_code == 200
    records_table.select.assert_called_once_with("*")
    summary_table.select.assert_called_once_with("price_paid, savings, offer_id")
    records_eq.gte.assert_called_once()
    gte_args = records_eq.gte.call_args.args
    assert gte_args[0] == "purchased_at"
    assert gte_args[1].endswith("+00:00")
    assert "interval" not in gte_args[1]
    assert "now()" not in gte_args[1]


@pytest.mark.asyncio
async def test_get_history_returns_summary_records():
    _test_app.dependency_overrides = _deps("user-77")
    transport = httpx.ASGITransport(app=_test_app)

    rows = [
        {
            "id": "r2",
            "list_id": "list-1",
            "list_item_id": "item-2",
            "item_name": "Pasta",
            "brand": "Rummo",
            "format_label": "500 g",
            "image_url": None,
            "category": "dispensa",
            "subcategory": "Primi Piatti e Preparati",
            "product_id": None,
            "offer_id": None,
            "supermarket_id": "store-2",
            "supermarket_name": "Coop",
            "quantity": 2,
            "price_paid": 1.5,
            "price_original": 2.0,
            "discount_pct": 25,
            "unit_price": "3,00 €/kg",
            "unit_price_value": 3.0,
            "unit_price_unit": "kg",
            "unit_price_label": "3,00 €/kg",
            "savings": 0.5,
            "purchased_at": "2026-05-08T09:00:00+00:00",
        },
        {
            "id": "r1",
            "list_id": "list-1",
            "list_item_id": "item-1",
            "item_name": "Latte",
            "brand": "Granarolo",
            "format_label": "1 L",
            "image_url": "https://example.com/latte.png",
            "category": "bevande",
            "subcategory": "Acqua e Bibite",
            "product_id": "prod-1",
            "offer_id": "offer-1",
            "supermarket_id": "store-1",
            "supermarket_name": "Esselunga",
            "quantity": 1,
            "price_paid": 1.2,
            "price_original": 1.6,
            "discount_pct": 25,
            "unit_price": "1,20 €/l",
            "unit_price_value": 1.2,
            "unit_price_unit": "l",
            "unit_price_label": "1,20 €/l",
            "savings": 0.4,
            "purchased_at": "2026-05-07T09:00:00+00:00",
        },
    ]

    sb = MagicMock()
    records_table = MagicMock()
    summary_table = MagicMock()
    sb.table.side_effect = [records_table, summary_table]

    records_select = records_table.select.return_value
    records_eq = records_select.eq.return_value
    records_gte = records_eq.gte.return_value
    records_order = records_gte.order.return_value
    records_order.order.return_value.limit.return_value.execute.return_value = MagicMock(data=rows)

    summary_select = summary_table.select.return_value
    summary_eq = summary_select.eq.return_value
    summary_eq.gte.return_value.execute.return_value = MagicMock(
        data=[
            {"price_paid": 1.5, "savings": 0.5, "offer_id": None},
            {"price_paid": 1.2, "savings": 0.4, "offer_id": "offer-1"},
        ]
    )

    with patch.object(_purchases_module, "get_supabase", return_value=sb):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/purchases/history", params={"days": 90})

    assert resp.status_code == 200
    assert resp.json() == {
        "total_savings": 0.9,
        "total_spend": 2.7,
        "total_purchases": 2,
        "total_offer_purchases": 1,
        "period_days": 90,
        "records": rows,
        "next_cursor_purchased_at": None,
        "next_cursor_id": None,
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_get_history_returns_next_cursor_and_applies_filters():
    _test_app.dependency_overrides = _deps("user-99")
    transport = httpx.ASGITransport(app=_test_app)

    rows = [
        {
            "id": "r3",
            "list_id": "list-1",
            "list_item_id": "item-3",
            "item_name": "Pasta",
            "brand": "Rummo",
            "format_label": "500 g",
            "image_url": None,
            "category": "dispensa",
            "subcategory": "Primi Piatti e Preparati",
            "product_id": None,
            "offer_id": "offer-3",
            "supermarket_id": "store-2",
            "supermarket_name": "Coop",
            "quantity": 1,
            "price_paid": 1.5,
            "price_original": 2.0,
            "discount_pct": 25,
            "unit_price": "3,00 €/kg",
            "unit_price_value": 3.0,
            "unit_price_unit": "kg",
            "unit_price_label": "3,00 €/kg",
            "savings": 0.5,
            "purchased_at": "2026-05-08T09:00:00+00:00",
        }
    ]

    sb = MagicMock()
    records_table = MagicMock()
    summary_table = MagicMock()
    sb.table.side_effect = [records_table, summary_table]

    records_select = records_table.select.return_value
    records_eq = records_select.eq.return_value
    records_gte = records_eq.gte.return_value
    records_category = records_gte.eq.return_value
    records_subcategory = records_category.eq.return_value
    records_supermarket = records_subcategory.eq.return_value
    records_source = records_supermarket.not_.is_.return_value
    records_source.order.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=rows + [dict(rows[0], id="r2", purchased_at="2026-05-07T09:00:00+00:00")]
    )

    summary_select = summary_table.select.return_value
    summary_eq = summary_select.eq.return_value
    summary_gte = summary_eq.gte.return_value
    summary_category = summary_gte.eq.return_value
    summary_subcategory = summary_category.eq.return_value
    summary_supermarket = summary_subcategory.eq.return_value
    summary_supermarket.not_.is_.return_value.execute.return_value = MagicMock(
        data=[
            {"price_paid": 1.5, "savings": 0.5, "offer_id": "offer-3"},
            {"price_paid": 1.2, "savings": 0.4, "offer_id": "offer-4"},
            {"price_paid": 2.1, "savings": 0.3, "offer_id": "offer-5"},
        ]
    )

    with patch.object(_purchases_module, "get_supabase", return_value=sb):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                "/purchases/history",
                params={
                    "days": 90,
                    "limit": 1,
                    "offset": 0,
                    "category": "dispensa",
                    "subcategory": "Primi Piatti e Preparati",
                    "supermarket": "Coop",
                    "source": "offer",
                },
            )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total_purchases"] == 3
    assert payload["total_offer_purchases"] == 3
    assert payload["total_spend"] == 4.8
    assert payload["total_savings"] == 1.2
    assert payload["next_cursor_purchased_at"] == "2026-05-08T09:00:00+00:00"
    assert payload["next_cursor_id"] == "r3"
    assert payload["has_more"] is True
