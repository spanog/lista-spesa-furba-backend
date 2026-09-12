import os

os.environ.setdefault("SUPABASE_SECRET_KEY", "test-secret-key")

import pytest
from api.routers.lists import (
    _patch_item_in_items,
    _patch_quantity_in_items,
    _stale_offer_ids_for_viewer,
)


def _make_item(item_id: str, quantity: float = 1.0) -> dict:
    return {
        "id": item_id,
        "name": "Test",
        "quantity": quantity,
        "checked": False,
        "purchased": False,
    }


def test_updates_quantity_for_matching_item():
    items = [_make_item("item-1", 1.0), _make_item("item-2", 1.0)]
    result = _patch_quantity_in_items(items, "item-1", 3.0)
    assert result[0]["quantity"] == 3.0
    assert result[1]["quantity"] == 1.0  # unchanged


def test_leaves_other_fields_intact():
    items = [_make_item("item-1", 1.0)]
    items[0]["name"] = "Latte"
    items[0]["checked"] = True
    result = _patch_quantity_in_items(items, "item-1", 2.0)
    assert result[0]["name"] == "Latte"
    assert result[0]["checked"] is True


def test_raises_404_when_item_not_found():
    from fastapi import HTTPException
    items = [_make_item("item-1")]
    with pytest.raises(HTTPException) as exc_info:
        _patch_quantity_in_items(items, "nonexistent", 2.0)
    assert exc_info.value.status_code == 404


def test_raises_422_when_quantity_below_one():
    from fastapi import HTTPException
    items = [_make_item("item-1")]
    with pytest.raises(HTTPException) as exc_info:
        _patch_quantity_in_items(items, "item-1", 0.0)
    assert exc_info.value.status_code == 422


def test_raises_422_when_quantity_negative():
    from fastapi import HTTPException
    items = [_make_item("item-1")]
    with pytest.raises(HTTPException) as exc_info:
        _patch_quantity_in_items(items, "item-1", -1.0)
    assert exc_info.value.status_code == 422


def test_patch_item_sets_selected_offer_snapshot():
    items = [
        {
            "id": "item-1",
            "name": "Latte",
            "quantity": 1.0,
            "source": "manual",
            "pinned_product_id": None,
            "pinned_offer_id": None,
            "found_deals": [],
        }
    ]
    offer_patch = {
        "source": "offer",
        "pinned_product_id": "prod-1",
        "pinned_offer_id": "offer-1",
        "category": "dairy",
        "subcategory": "Latte",
        "found_deals": [
            {
                "offer_id": "offer-1",
                "product_id": "prod-1",
                "product_name": "Latte intero",
                "supermarket_id": "store-1",
                "supermarket_name": "Lidl",
                "price_offer": 0.99,
            }
        ],
    }

    result = _patch_item_in_items(items, "item-1", offer_patch)

    assert result[0]["source"] == "offer"
    assert result[0]["pinned_offer_id"] == "offer-1"
    assert result[0]["pinned_product_id"] == "prod-1"
    assert result[0]["found_deals"][0]["offer_id"] == "offer-1"
    assert result[0]["found_deals"][0]["supermarket_name"] == "Lidl"


def test_patch_item_clears_subcategory_when_category_is_null():
    items = [
        {
            "id": "item-1",
            "name": "Latte",
            "quantity": 1.0,
            "category": "alimentari-freschi",
            "subcategory": "Latticini e Formaggi",
        }
    ]

    result = _patch_item_in_items(items, "item-1", {"category": None})

    assert result[0]["category"] is None
    assert result[0]["subcategory"] is None


def test_current_offer_with_legacy_inactive_cache_is_not_stale():
    items = [{"pinned_offer_id": "offer-1"}]
    offer_rows = [{
        "id": "offer-1",
        "valid_from": "2000-01-01",
        "valid_to": "2099-12-31",
        "is_active": False,
    }]

    stale = _stale_offer_ids_for_viewer(items, offer_rows, set())

    assert stale == set()


# ---------------------------------------------------------------------------
# Endpoint tests — PATCH /lists/{list_id}/items/{item_id}
# ---------------------------------------------------------------------------

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

# Stub infrastructure (same pattern as test_favorites_router.py)
for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock()
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

_auth_mod = types.ModuleType("core.auth")
_auth_mod.get_current_access_token = MagicMock()
_auth_mod.get_current_user_id = MagicMock()
sys.modules["core.auth"] = _auth_mod

import httpx
from fastapi import FastAPI
import api.routers.lists as _lists_module
from api.routers.lists import router as _lists_router

_lists_module._publish_list_sync_event = MagicMock()

_DEP_GET_USER_ID = _lists_module.get_current_user_id
_DEP_GET_ACCESS_TOKEN = _lists_module.get_current_access_token

_test_app = FastAPI()
_test_app.include_router(_lists_router, prefix="/lists")

_LIST_ID = "list-abc"
_ITEM_ID = "item-1"
_USER_ID = "user-xyz"


def _deps(user_id: str = _USER_ID) -> dict:
    return {
        _DEP_GET_USER_ID: lambda: user_id,
        _DEP_GET_ACCESS_TOKEN: lambda: "test-access-token",
    }


async def _patch_req(url: str, json: dict, dep_overrides: dict | None = None) -> httpx.Response:
    _test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(url, json=json)


async def _post_req(
    url: str,
    json: dict,
    dep_overrides: dict | None = None,
) -> httpx.Response:
    _test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json=json)


async def _delete_req(
    url: str,
    dep_overrides: dict | None = None,
) -> httpx.Response:
    _test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.delete(url)


async def _get_req(
    url: str,
    dep_overrides: dict | None = None,
) -> httpx.Response:
    _test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def test_patch_quantity_returns_updated_item():
    initial_items = [
        {"id": _ITEM_ID, "name": "Latte", "quantity": 1.0, "checked": False, "purchased": False}
    ]
    updated_items = [
        {"id": _ITEM_ID, "name": "Latte", "quantity": 3.0, "checked": False, "purchased": False}
    ]
    sb_mock = MagicMock()
    table = sb_mock.table.return_value
    table.select.return_value.eq.return_value.single.return_value.execute.side_effect = [
        MagicMock(data={"items": initial_items}),
        MagicMock(data={"items": updated_items}),
    ]

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(_lists_module, "_rpc_update_list_item", new=AsyncMock()) as rpc_mock:
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={"quantity": 3.0},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 200
    assert resp.json()["quantity"] == 3.0
    assert resp.json()["id"] == _ITEM_ID
    rpc_mock.assert_awaited_once_with(
        _LIST_ID,
        _ITEM_ID,
        {"quantity": 3.0},
        _USER_ID,
        "test-access-token",
    )


async def test_clear_stale_offers_uses_update_list_item_rpc_patch():
    list_items = [
        {
            "id": _ITEM_ID,
            "name": "Caciocavallo Silano D.O.P. Campolongo",
            "pinned_offer_id": "offer-1",
            "pinned_product_id": "prod-1",
            "found_deals": [{"offer_id": "offer-1", "price_offer": 1.69}],
        }
    ]
    shopping_lists_table = MagicMock()
    shopping_lists_table.select.return_value.eq.return_value.single.return_value.execute.return_value = (
        MagicMock(data={"items": list_items})
    )
    offers_table = MagicMock()
    offers_table.select.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "offer-1",
                "price_offer": 1.69,
                "valid_to": "2026-06-04",
                "is_active": False,
            }
        ]
    )
    sb_mock = MagicMock()
    sb_mock.table.side_effect = (
        lambda table_name: {
            "shopping_lists": shopping_lists_table,
            "offers": offers_table,
        }[table_name]
    )

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(_lists_module, "_hidden_offer_ids_for_viewer", return_value=set()), \
         patch.object(_lists_module, "_rpc_update_list_item", new=AsyncMock()) as rpc_mock, \
         patch.object(_lists_module, "_publish_list_sync_event") as publish_mock:
        resp = await _post_req(
            f"/lists/{_LIST_ID}/clear-stale-offers",
            json={},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "cleared": 1,
        "cleared_names": ["Caciocavallo Silano D.O.P. Campolongo"],
    }
    rpc_mock.assert_awaited_once_with(
        _LIST_ID,
        _ITEM_ID,
        {"pinned_offer_id": None, "found_deals": []},
        _USER_ID,
        "test-access-token",
    )
    publish_mock.assert_called_once_with(
        _LIST_ID,
        "list_updated",
        "stale_offers_cleared",
    )


async def test_patch_selected_offer_returns_coherent_item():
    initial_items = [
        {
            "id": _ITEM_ID,
            "name": "Latte",
            "quantity": 1.0,
            "source": "manual",
            "pinned_product_id": None,
            "pinned_offer_id": None,
            "found_deals": [],
        }
    ]
    offer_patch = {
        "source": "offer",
        "pinned_product_id": "prod-1",
        "pinned_offer_id": "offer-1",
        "category": "dairy",
        "subcategory": "Latte",
        "found_deals": [{"offer_id": "offer-1", "price_offer": 0.99}],
    }
    updated_items = [{**initial_items[0], **offer_patch}]
    sb_mock = MagicMock()
    table = sb_mock.table.return_value
    table.select.return_value.eq.return_value.single.return_value.execute.side_effect = [
        MagicMock(data={"items": initial_items}),
        MagicMock(data={"items": updated_items}),
    ]

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(_lists_module, "_selected_offer_patch", return_value=offer_patch), \
         patch.object(_lists_module, "_rpc_update_list_item", new=AsyncMock()) as rpc_mock:
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={"pinned_offer_id": "offer-1"},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 200
    assert resp.json()["source"] == "offer"
    assert resp.json()["pinned_offer_id"] == "offer-1"
    assert resp.json()["pinned_product_id"] == "prod-1"
    assert resp.json()["found_deals"][0]["offer_id"] == "offer-1"
    rpc_mock.assert_awaited_once_with(
        _LIST_ID,
        _ITEM_ID,
        offer_patch,
        _USER_ID,
        "test-access-token",
    )


async def test_list_members_flattens_profile_and_email_fields():
    members_table = MagicMock()
    profiles_table = MagicMock()
    members_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "member-row-1",
                "list_id": _LIST_ID,
                "user_id": _USER_ID,
                "role": "owner",
                "invited_by": None,
                "joined_at": "2026-05-11T10:00:00.000Z",
            }
        ]
    )
    profiles_table.select.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": _USER_ID,
                "display_name": "Mario Rossi",
                "avatar_url": "https://example.com/avatar.jpg",
            }
        ]
    )

    sb_mock = MagicMock()
    sb_mock.table.side_effect = lambda name: {
        "list_members": members_table,
        "user_profiles": profiles_table,
    }[name]
    sb_mock.auth.admin.get_user_by_id.return_value = MagicMock(
        user=MagicMock(email="mario@example.com")
    )

    with (
        patch.object(_lists_module, "get_supabase", return_value=sb_mock),
        patch.object(_lists_module, "_verify_member", return_value=None),
    ):
        resp = await _get_req(f"/lists/{_LIST_ID}/members", dep_overrides=_deps())

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "id": "member-row-1",
            "list_id": _LIST_ID,
            "user_id": _USER_ID,
            "role": "owner",
            "invited_by": None,
            "joined_at": "2026-05-11T10:00:00.000Z",
            "display_name": "Mario Rossi",
            "avatar_url": "https://example.com/avatar.jpg",
            "email": "mario@example.com",
        }
    ]


async def test_add_offer_item_returns_brand_in_snapshot():
    sb_mock = MagicMock()

    offer_patch = {
        "source": "offer",
        "name": "Bistecca scelta",
        "brand": "Filiera Italia",
        "pinned_product_id": "prod-2",
        "pinned_offer_id": "offer-2",
        "image_url": None,
        "category": "alimentari-freschi",
        "subcategory": "Macelleria e Polleria",
        "found_deals": [{"offer_id": "offer-2", "price_offer": 9.9}],
    }

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(_lists_module, "_selected_offer_patch", return_value=offer_patch), \
         patch.object(_lists_module, "_enrich_items_with_categories", return_value=[offer_patch]), \
         patch.object(_lists_module, "_rpc_append_list_item", new=AsyncMock()) as rpc_mock:
        resp = await _post_req(
            f"/lists/{_LIST_ID}/items",
            json={
                "name": "Bistecca scelta",
                "brand": "Filiera Italia",
                "quantity": 1,
                "source": "offer",
                "pinned_product_id": "prod-2",
                "pinned_offer_id": "offer-2",
                "image_url": None,
            },
            dep_overrides=_deps(),
        )

    assert resp.status_code == 201
    assert resp.json()["brand"] == "Filiera Italia"
    rpc_mock.assert_awaited_once()


async def test_patch_category_returns_updated_item():
    initial_items = [
        {
            "id": _ITEM_ID,
            "name": "Latte",
            "quantity": 1.0,
            "source": "manual",
            "category": None,
            "subcategory": None,
        }
    ]
    updated_items = [
        {
            **initial_items[0],
            "category": "alimentari-freschi",
            "subcategory": "Latticini e Formaggi",
        }
    ]
    sb_mock = MagicMock()
    sb_mock.table.return_value.select.return_value.eq.return_value.single.return_value.execute.side_effect = [
        MagicMock(data={"items": initial_items}),
        MagicMock(data={"items": updated_items}),
    ]

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(_lists_module, "_rpc_update_list_item", new=AsyncMock()):
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={
                "category": "alimentari-freschi",
                "subcategory": "Latticini e Formaggi",
            },
            dep_overrides=_deps(),
        )

    assert resp.status_code == 200
    assert resp.json()["category"] == "alimentari-freschi"
    assert resp.json()["subcategory"] == "Latticini e Formaggi"


async def test_patch_selected_offer_404_does_not_call_rpc():
    initial_items = [
        {
            "id": _ITEM_ID,
            "name": "Latte",
            "quantity": 1.0,
            "source": "manual",
            "pinned_product_id": None,
            "pinned_offer_id": None,
            "found_deals": [],
        }
    ]
    sb_mock = MagicMock()
    sb_mock.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "items": initial_items
    }

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(
             _lists_module,
             "_selected_offer_patch",
             side_effect=HTTPException(status_code=404, detail="Offer not found"),
         ), \
         patch.object(_lists_module, "_rpc_update_list_item", new=AsyncMock()) as rpc_mock:
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={"pinned_offer_id": "missing"},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 404
    rpc_mock.assert_not_awaited()


async def test_patch_category_422_on_invalid_category():
    sb_mock = MagicMock()
    sb_mock.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "items": [{"id": _ITEM_ID, "name": "Latte", "quantity": 1.0}]
    }

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None):
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={"category": "non-esiste"},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 422


async def test_patch_category_422_on_invalid_subcategory():
    sb_mock = MagicMock()
    sb_mock.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "items": [{"id": _ITEM_ID, "name": "Latte", "quantity": 1.0}]
    }

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None):
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={
                "category": "alimentari-freschi",
                "subcategory": "Acqua e Bibite",
            },
            dep_overrides=_deps(),
        )

    assert resp.status_code == 422


async def test_patch_quantity_422_on_zero():
    sb_mock = MagicMock()
    sb_mock.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "items": [{"id": _ITEM_ID, "name": "Latte", "quantity": 1.0, "checked": False, "purchased": False}]
    }
    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None):
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={"quantity": 0},
            dep_overrides=_deps(),
        )
    assert resp.status_code == 422


async def test_patch_quantity_403_non_member():
    from fastapi import HTTPException
    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_lists_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member")):
        resp = await _patch_req(
            f"/lists/{_LIST_ID}/items/{_ITEM_ID}",
            json={"quantity": 2.0},
            dep_overrides=_deps(),
        )
    assert resp.status_code == 403


async def test_reset_list_clears_items_and_returns_updated_list():
    updated_list = {
        "id": _LIST_ID,
        "user_id": _USER_ID,
        "name": "Lista principale",
        "items": [],
        "is_active": True,
    }
    sb_mock = MagicMock()
    table = sb_mock.table.return_value
    table.select.return_value.eq.return_value.single.return_value.execute.return_value.data = updated_list

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None):
        resp = await _post_req(
            f"/lists/{_LIST_ID}/reset",
            json={},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 200
    assert resp.json()["items"] == []
    sb_mock.table.return_value.update.assert_called_with({"items": []})


async def test_reset_list_403_non_member():
    from fastapi import HTTPException
    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_lists_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member")):
        resp = await _post_req(
            f"/lists/{_LIST_ID}/reset",
            json={},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 403


async def test_remove_purchased_items_clears_only_purchased_items_and_returns_updated_list():
    updated_list = {
        "id": _LIST_ID,
        "user_id": _USER_ID,
        "name": "Lista principale",
        "items": [
            {
                "id": "item-2",
                "name": "Pane",
                "quantity": 1,
                "purchased": False,
            }
        ],
        "is_active": True,
    }
    sb_mock = MagicMock()
    table = sb_mock.table.return_value
    table.select.return_value.eq.return_value.single.return_value.execute.side_effect = [
        MagicMock(
            data={
                "items": [
                    {
                        "id": "item-1",
                        "name": "Latte",
                        "quantity": 1,
                        "purchased": True,
                    },
                    {
                        "id": "item-2",
                        "name": "Pane",
                        "quantity": 1,
                        "purchased": False,
                    },
                ]
            }
        ),
        MagicMock(data=updated_list),
    ]

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None):
        resp = await _post_req(
            f"/lists/{_LIST_ID}/items/remove-purchased",
            json={},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 200
    assert resp.json()["items"] == [
        {
            "id": "item-2",
            "name": "Pane",
            "quantity": 1,
            "purchased": False,
            "brand": None,
            "category": None,
            "subcategory": None,
        }
    ]
    sb_mock.table.return_value.update.assert_called_with(
        {"items": updated_list["items"]}
    )


async def test_remove_purchased_items_403_non_member():
    from fastapi import HTTPException

    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_lists_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member")):
        resp = await _post_req(
            f"/lists/{_LIST_ID}/items/remove-purchased",
            json={},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_add_item_403_non_member():
    from fastapi import HTTPException

    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_lists_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member")):
        resp = await _post_req(
            f"/lists/{_LIST_ID}/items",
            json={"name": "Latte"},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_toggle_item_403_non_member():
    from fastapi import HTTPException

    _test_app.dependency_overrides = _deps()
    transport = httpx.ASGITransport(app=_test_app)
    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_lists_module, "_verify_member", side_effect=HTTPException(status_code=403, detail="Not a member")):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/lists/{_LIST_ID}/items/{_ITEM_ID}/toggle")

    assert resp.status_code == 403


def test_notify_invited_user_sends_push_for_each_subscription():
    sb_mock = MagicMock()

    with patch.object(_lists_module, "send_push_to_user") as push_mock:
        _lists_module._notify_invited_user(
            sb_mock,
            "user-1",
            "Lista rimossa",
            "Owner ha rimosso lista Weekend",
            {"list_id": "list-1", "url": "/lista"},
        )

    push_mock.assert_called_once()
    push_kwargs = push_mock.call_args.kwargs
    assert push_kwargs["user_id"] == "user-1"
    assert push_kwargs["title"] == "Lista rimossa"
    assert push_kwargs["body"] == "Owner ha rimosso lista Weekend"
    assert push_kwargs["data"]["list_id"] == "list-1"


def test_shared_list_event_keeps_inbox_when_push_disabled():
    sb_mock = MagicMock()
    with patch.object(_lists_module, "_create_app_notification") as create_notification_mock, \
         patch.object(_lists_module, "_notify_invited_user") as notify_mock:
        create_notification_mock.return_value = {"id": "notif-1"}
        result = _lists_module._notify_shared_list_event(
            sb_mock,
            "user-1",
            kind="list_member_removed",
            title="Rimosso dalla lista",
            body="Owner ti ha rimosso",
            data={"list_id": "list-1", "url": "/lista"},
        )

    assert result == {"id": "notif-1"}
    create_notification_mock.assert_called_once()
    notify_mock.assert_called_once()


@pytest.mark.asyncio
async def test_member_can_leave_shared_list_and_notify_owner():
    sb_mock = MagicMock()

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_existing_member", return_value=True), \
         patch.object(_lists_module, "_list_member_role", return_value="member"), \
         patch.object(
             _lists_module,
             "_shopping_list_row",
             return_value={"id": "list-1", "name": "Weekend", "user_id": "owner-1"},
         ), \
         patch.object(_lists_module, "_delete_member") as delete_member_mock, \
         patch.object(_lists_module, "_fallback_selected_list_for_users") as fallback_mock, \
         patch.object(
             _lists_module,
             "_profile_row",
             return_value={"display_name": "Mario"},
         ) as profile_mock, \
         patch.object(
             _lists_module,
             "_auth_user_by_id",
             return_value={"id": "member-1", "email": "mario@example.com"},
         ) as auth_user_mock, \
         patch.object(_lists_module, "_notify_shared_list_event") as notify_shared_list_event_mock, \
         patch.object(_lists_module, "_verify_owner") as verify_owner_mock:
        resp = await _delete_req(
            "/lists/list-1/members/member-1",
            dep_overrides=_deps("member-1"),
        )

    assert resp.status_code == 204
    delete_member_mock.assert_called_once_with("list-1", "member-1")
    fallback_mock.assert_called_once_with(sb_mock, {"member-1"}, "list-1")
    verify_owner_mock.assert_not_called()
    profile_mock.assert_called_once_with(sb_mock, "member-1")
    auth_user_mock.assert_called_once_with("member-1")
    notify_shared_list_event_mock.assert_called_once_with(
        sb_mock,
        "owner-1",
        kind="list_member_left",
        title="Membro uscito dalla lista",
        body="Mario ha lasciato la lista",
        data={
            "list_id": "list-1",
            "list_name": "Weekend",
            "left_by": "Mario",
            "left_by_email": "mario@example.com",
            "url": "/lista",
        },
    )


@pytest.mark.asyncio
async def test_owner_cannot_leave_own_list_endpoint():
    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(_lists_module, "_existing_member", return_value=True), \
         patch.object(_lists_module, "_list_member_role", return_value="owner"):
        resp = await _delete_req(
            "/lists/list-1/members/owner-1",
            dep_overrides=_deps("owner-1"),
        )

    assert resp.status_code == 400
