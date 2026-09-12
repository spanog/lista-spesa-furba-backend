"""Unit tests for api/routers/notifications.py."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

_auth_mod = types.ModuleType("core.auth")
_auth_mod.get_current_user_id = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.auth"] = _auth_mod

from fastapi import FastAPI
import httpx
import pytest

import api.routers.notifications as _notifications_module
from api.routers.notifications import router

_DEP_GET_USER_ID = _notifications_module.get_current_user_id

notifications_app = FastAPI()
notifications_app.include_router(router, prefix="/notifications")

USER_ID = "user-abc"
NOTIFICATION_ID = "11111111-1111-1111-1111-111111111111"


def _deps(user_id: str = USER_ID) -> dict:
    return {_DEP_GET_USER_ID: lambda: user_id}


async def _delete(url: str, dep_overrides: dict | None = None) -> httpx.Response:
    notifications_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=notifications_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.delete(url)


async def _post(
    url: str,
    dep_overrides: dict | None = None,
    json: dict | None = None,
) -> httpx.Response:
    notifications_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=notifications_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json=json)


class TestDeleteNotification:
    @pytest.mark.asyncio
    async def test_returns_204_and_scopes_delete_to_current_user(self):
        delete_notification = MagicMock(return_value=False)

        with patch.object(
            _notifications_module.repo,
            "delete_notification",
            delete_notification,
            create=True,
        ):
            response = await _delete(f"/notifications/{NOTIFICATION_ID}", _deps())

        assert response.status_code == 204
        assert response.content == b""
        delete_notification.assert_called_once_with(NOTIFICATION_ID, USER_ID)

    @pytest.mark.asyncio
    async def test_rejects_malformed_uuid_path_id(self):
        response = await _delete("/notifications/not-a-uuid", _deps())

        assert response.status_code == 422


class TestDeleteManyNotifications:
    @pytest.mark.asyncio
    async def test_returns_deleted_and_missing_ids(self):
        delete_notifications = MagicMock(return_value={
            "deleted_ids": ["11111111-1111-1111-1111-111111111111"],
            "missing_ids": ["22222222-2222-2222-2222-222222222222"],
        })

        with patch.object(
            _notifications_module.repo,
            "delete_notifications",
            delete_notifications,
            create=True,
        ):
            response = await _post(
                "/notifications/delete-many",
                _deps(),
                json={
                    "notification_ids": [
                        "11111111-1111-1111-1111-111111111111",
                        "22222222-2222-2222-2222-222222222222",
                    ]
                },
            )

        assert response.status_code == 200
        assert response.json() == {
            "deleted_ids": ["11111111-1111-1111-1111-111111111111"],
            "missing_ids": ["22222222-2222-2222-2222-222222222222"],
        }
        delete_notifications.assert_called_once_with(
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
            USER_ID,
        )

    @pytest.mark.asyncio
    async def test_rejects_empty_notification_ids(self):
        response = await _post(
            "/notifications/delete-many",
            _deps(),
            json={"notification_ids": []},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_malformed_uuid_entries(self):
        response = await _post(
            "/notifications/delete-many",
            _deps(),
            json={"notification_ids": ["11111111-1111-1111-1111-111111111111", "not-a-uuid"]},
        )

        assert response.status_code == 422
