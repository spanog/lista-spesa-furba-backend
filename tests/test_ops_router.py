from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = types.SimpleNamespace(ops_cron_secret="test-ops-secret")
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

import api.routers.ops as _ops_module
from api.routers.ops import router as _ops_router

_test_app = FastAPI()
_test_app.include_router(_ops_router, prefix="/ops")


async def _post(
    secret: str | None = None,
    path: str = "/ops/cron/daily-maintenance",
) -> httpx.Response:
    headers = {}
    if secret is not None:
        headers["x-ops-secret"] = secret
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=headers)


@pytest.mark.asyncio
async def test_daily_maintenance_requires_matching_secret():
    response = await _post("wrong-secret")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid ops secret"


@pytest.mark.asyncio
async def test_daily_maintenance_rejects_when_secret_not_configured():
    with patch.object(_ops_module.settings, "ops_cron_secret", ""):
        response = await _post("test-ops-secret")

    assert response.status_code == 503
    assert response.json()["detail"] == "Ops cron secret is not configured"


@pytest.mark.asyncio
async def test_daily_maintenance_runs_both_cleanup_services():
    flyer_cleanup = MagicMock(return_value=7)
    purchased_cleanup = MagicMock(return_value=3)

    with (
        patch.object(_ops_module.FlyerCleanupService, "run", flyer_cleanup),
        patch.object(_ops_module.PurchasedItemsCleanupService, "run", purchased_cleanup),
    ):
        response = await _post("test-ops-secret")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "deleted_offers": 7,
        "removed_purchased_items": 3,
        "errors": [],
    }
    flyer_cleanup.assert_called_once_with()
    purchased_cleanup.assert_called_once_with()


@pytest.mark.asyncio
async def test_daily_maintenance_returns_partial_error_when_one_cleanup_crashes():
    flyer_cleanup = MagicMock(side_effect=RuntimeError("boom"))
    purchased_cleanup = MagicMock(return_value=3)

    with (
        patch.object(_ops_module.FlyerCleanupService, "run", flyer_cleanup),
        patch.object(_ops_module.PurchasedItemsCleanupService, "run", purchased_cleanup),
    ):
        response = await _post("test-ops-secret")

    assert response.status_code == 200
    assert response.json() == {
        "status": "partial_error",
        "deleted_offers": 0,
        "removed_purchased_items": 3,
        "errors": ["flyer_cleanup"],
    }
    flyer_cleanup.assert_called_once_with()
    purchased_cleanup.assert_called_once_with()


@pytest.mark.asyncio
async def test_notifications_cron_runs_notification_worker():
    worker = MagicMock(return_value={"claimed": 4, "processed": 3, "failed": 1})

    with patch.object(_ops_module.NotificationJobWorker, "run_pending", worker):
        response = await _post("test-ops-secret", path="/ops/cron/notifications")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "claimed": 4,
        "processed": 3,
        "failed": 1,
    }
    worker.assert_called_once_with()
