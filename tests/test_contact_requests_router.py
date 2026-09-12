"""Unit tests for api/routers/contact_requests.py."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_settings = MagicMock()
_config_mod.settings = _settings  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod

_db_mod = types.ModuleType("core.database")
_db_mod.get_supabase = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.database"] = _db_mod

_auth_mod = types.ModuleType("core.auth")
_auth_mod.get_optional_user = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.auth"] = _auth_mod

import api.routers.contact_requests as _module
from api.routers.contact_requests import router
from services.contact_requests import ContactRequestConfigurationError, ContactRequestDeliveryError

test_app = FastAPI()
test_app.include_router(router, prefix="/contact-requests")

_DEP_OPTIONAL_USER = _module.get_optional_user


async def _post(data: dict, files: list[tuple] | None = None, user: dict | None = None) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_OPTIONAL_USER: lambda: user}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/contact-requests", data=data, files=files)


@pytest.mark.asyncio
async def test_bug_report_accepted_for_guest_with_screenshots():
    service = MagicMock()
    service.submit_bug_report = AsyncMock(return_value={"status": "sent"})
    with patch.object(_module, "_build_service", return_value=service) as build_service:
        response = await _post(
            data={
                "request_type": "bug_report",
                "email": "guest@example.com",
                "subject": "Bug checkout",
                "message": "Pagina bloccata dopo il click finale.",
            },
            files=[("screenshots", ("bug.png", b"png", "image/png"))],
        )

    assert response.status_code == 201
    assert response.json() == {"status": "sent"}
    build_service.assert_called_once_with()


@pytest.mark.asyncio
async def test_bug_report_accepted_for_guest_without_screenshots():
    service = MagicMock()
    service.submit_bug_report = AsyncMock(return_value={"status": "sent"})
    with patch.object(_module, "_build_service", return_value=service) as build_service:
        response = await _post(
            data={
                "request_type": "bug_report",
                "email": "guest@example.com",
                "subject": "Bug checkout",
                "message": "Pagina bloccata dopo il click finale.",
                "page_url": "login mobile safari",
            },
        )

    assert response.status_code == 201
    assert response.json() == {"status": "sent"}
    build_service.assert_called_once_with()


@pytest.mark.asyncio
async def test_collaboration_request_accepted_for_authenticated_user():
    service = MagicMock()
    service.submit_collaboration_request = AsyncMock(return_value={"status": "sent"})
    with patch.object(_module, "_build_service", return_value=service):
        response = await _post(
            data={
                "request_type": "collaboration_request",
                "email": "manager@example.com",
                "contact_name": "Mario Rossi",
                "supermarket_name": "Coop",
                "location": "Milano",
                "message": "Vorrei parlare della presenza del nostro punto vendita.",
            },
            user={"sub": "user-1", "email": "session@example.com"},
        )

    assert response.status_code == 201
    assert response.json() == {"status": "sent"}


@pytest.mark.asyncio
async def test_feature_request_accepted_for_authenticated_user():
    service = MagicMock()
    service.submit_feature_request = AsyncMock(return_value={"status": "sent"})
    with patch.object(_module, "_build_service", return_value=service):
        response = await _post(
            data={
                "request_type": "feature_request",
                "email": "user@example.com",
                "subject": "Filtri salvati",
                "message": "Vorrei salvare i filtri usati piu spesso nella pagina offerte.",
                "page_url": "offerte",
            },
            user={"sub": "user-1", "email": "session@example.com"},
        )

    assert response.status_code == 201
    assert response.json() == {"status": "sent"}


@pytest.mark.asyncio
async def test_missing_flyer_request_accepted_without_email():
    service = MagicMock()
    service.submit_missing_flyer_request = AsyncMock(return_value={"status": "sent"})
    with patch.object(_module, "_build_service", return_value=service):
        response = await _post(
            data={
                "request_type": "missing_flyer_request",
                "city": "Roma",
                "supermarket": "Lidl",
            }
        )

    assert response.status_code == 201
    assert response.json() == {"status": "sent"}


@pytest.mark.asyncio
async def test_missing_required_field_returns_422():
    response = await _post(
        data={
            "request_type": "collaboration_request",
            "email": "user@example.com",
            "contact_name": "Mario Rossi",
        }
    )

    assert response.status_code == 422
    assert "supermarket_name" in response.json()["detail"]


@pytest.mark.asyncio
async def test_subject_with_crlf_returns_422():
    response = await _post(
        data={
            "request_type": "bug_report",
            "email": "user@example.com",
            "subject": "Bug\r\nBcc: attacker@example.com",
            "message": "Pagina bloccata dopo il click finale.",
        }
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_configuration_errors_return_503():
    service = MagicMock()
    service.submit_missing_flyer_request = AsyncMock(
        side_effect=ContactRequestConfigurationError("Missing contact mail configuration: webmaster_email")
    )
    with patch.object(_module, "_build_service", return_value=service), patch.object(_module, "logger") as logger:
        response = await _post(
            data={
                "request_type": "missing_flyer_request",
                "city": "Napoli",
            }
        )

    assert response.status_code == 503
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_delivery_errors_return_500_and_log_exception():
    service = MagicMock()
    service.submit_missing_flyer_request = AsyncMock(
        side_effect=ContactRequestDeliveryError("Failed to connect to SMTP server")
    )
    with patch.object(_module, "_build_service", return_value=service), patch.object(_module, "logger") as logger:
        response = await _post(
            data={
                "request_type": "missing_flyer_request",
                "city": "Napoli",
            }
        )

    assert response.status_code == 500
    logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_bug_report_rejects_more_than_three_screenshots():
    response = await _post(
        data={
            "request_type": "bug_report",
            "email": "guest@example.com",
            "subject": "Bug checkout",
            "message": "Pagina bloccata dopo il click finale.",
        },
        files=[
            ("screenshots", ("uno.png", b"1", "image/png")),
            ("screenshots", ("due.png", b"2", "image/png")),
            ("screenshots", ("tre.png", b"3", "image/png")),
            ("screenshots", ("quattro.png", b"4", "image/png")),
        ],
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Too many screenshots: max 3"}
