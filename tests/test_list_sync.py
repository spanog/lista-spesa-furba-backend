from __future__ import annotations

import sys
import types
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock(
    supabase_url="http://supabase.local",
    supabase_secret_key="service-role",
)
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

_auth_mod = types.ModuleType("core.auth")
_auth_mod.get_current_access_token = MagicMock()
_auth_mod.get_current_user_id = MagicMock()
sys.modules["core.auth"] = _auth_mod

from services import list_sync
import api.routers.lists as _lists_module
from api.routers.lists import router as _lists_router

_DEP_GET_USER_ID = _lists_module.get_current_user_id
_DEP_GET_ACCESS_TOKEN = _lists_module.get_current_access_token

_test_app = FastAPI()
_test_app.include_router(_lists_router, prefix="/lists")


def _deps(user_id: str = "user-1") -> dict:
    return {
        _DEP_GET_USER_ID: lambda: user_id,
        _DEP_GET_ACCESS_TOKEN: lambda: "test-access-token",
    }


async def _get(url: str, dep_overrides: dict | None = None) -> httpx.Response:
    _test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def _post(
    url: str,
    json: dict,
    dep_overrides: dict | None = None,
) -> httpx.Response:
    _test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json=json)


def test_publish_list_sync_event_uses_pg_notify(monkeypatch):
    cursor = MagicMock()

    class _CursorCtx:
        def __enter__(self):
            return cursor

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(list_sync, "has_direct_postgres", lambda: True)
    monkeypatch.setattr(list_sync, "get_postgres_cursor", lambda: _CursorCtx())

    list_sync.publish_list_sync_event("list-1", "list_updated", "item_added")

    cursor.execute.assert_called_once()
    args = cursor.execute.call_args.args
    assert args[0] == "SELECT pg_notify(%s, %s)"
    assert args[1][0] == list_sync.LIST_SYNC_CHANNEL
    assert '"reason": "item_added"' in args[1][1]


def test_parse_list_sync_event_rejects_invalid_payload():
    assert list_sync.parse_list_sync_event("not-json") is None
    assert list_sync.parse_list_sync_event('{"event":"wrong"}') is None


@pytest.mark.asyncio
async def test_stream_list_events_requires_membership():
    with patch.object(_lists_module, "has_direct_postgres", return_value=True), \
         patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
         patch.object(
             _lists_module,
             "_verify_member",
             side_effect=HTTPException(status_code=403, detail="Not a member of this list"),
         ):
        resp = await _get("/lists/list-1/events", dep_overrides=_deps())

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_add_item_publishes_list_sync_event():
    sb_mock = MagicMock()

    with patch.object(_lists_module, "get_supabase", return_value=sb_mock), \
         patch.object(_lists_module, "_verify_member", return_value=None), \
         patch.object(_lists_module, "_rpc_append_list_item", new=AsyncMock()), \
         patch.object(
             _lists_module,
             "_enrich_items_with_categories",
             return_value=[{"id": "item-1", "name": "Latte", "found_deals": []}],
         ), \
         patch.object(_lists_module, "_publish_list_sync_event") as publish_mock:
        resp = await _post(
            "/lists/list-1/items",
            json={"name": "Latte", "quantity": 1},
            dep_overrides=_deps(),
        )

    assert resp.status_code == 201
    publish_mock.assert_called_once_with("list-1", "list_updated", "item_added")


@pytest.mark.asyncio
async def test_accept_invite_publishes_members_and_invites_updates():
    invite = {
        "id": "invite-1",
        "list_id": "list-1",
        "invited_by": "owner-1",
        "status": "pending",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }

    with patch.object(_lists_module, "get_supabase", return_value=MagicMock()), \
        patch.object(_lists_module, "_invite_for_user", return_value=invite), \
        patch.object(_lists_module, "_existing_member", return_value=False), \
        patch.object(_lists_module, "_insert_member"), \
        patch.object(
            _lists_module,
            "_shopping_list_row",
            return_value={"id": "list-1", "name": "Weekend"},
        ), \
        patch.object(
            _lists_module,
            "_profile_row",
            return_value={"display_name": "Giulia"},
        ), \
        patch.object(_lists_module, "_set_invite_status"), \
        patch.object(_lists_module, "_mark_invite_notifications_read"), \
        patch.object(_lists_module, "_notify_shared_list_event") as notification_mock, \
        patch.object(_lists_module, "_publish_list_sync_event") as publish_mock:
        resp = await _post(
            "/lists/invites/invite-1/accept",
            json={},
            dep_overrides=_deps("member-1"),
        )

    assert resp.status_code == 200
    notification_mock.assert_called_once_with(
        ANY,
        "owner-1",
        kind="list_invite_accepted",
        title="Invito accettato",
        body="Giulia ha accettato il tuo invito: adesso condivide la tua lista",
        data={
            "list_id": "list-1",
            "list_name": "Weekend",
            "accepted_by": "Giulia",
            "url": "/lista",
        },
    )
    assert publish_mock.call_args_list == [
        (("list-1", "members_updated", "member_joined"),),
        (("list-1", "invites_updated", "invite_accepted"),),
    ]
