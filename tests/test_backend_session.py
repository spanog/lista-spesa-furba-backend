"""Tests for backend-owned session JWT primitives."""

from __future__ import annotations

from types import SimpleNamespace
import importlib
import os
import sys
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Restore real jose and reimport core.session if another test file installed
# module-level mocks (e.g. test_auth_manager.py stubs jose globally).
for _jose_key in ["jose", "jose.jwt", "jose.exceptions", "jose.backends"]:
    if isinstance(sys.modules.get(_jose_key), MagicMock):
        del sys.modules[_jose_key]
for _sess_key in [k for k in list(sys.modules) if k.startswith("core.session")]:
    del sys.modules[_sess_key]

import core.session as session
import core.guest_location as guest_location
from core.guest_location import GUEST_LOCATION_RADIUS_KM, create_guest_location_token, read_guest_location
from api.routers.guest_location import router as guest_location_router
import api.routers.guest_location as guest_location_router_module


guest_location_app = FastAPI()
guest_location_app.include_router(guest_location_router, prefix="/guest-location")


@guest_location_app.get("/guest-location/cookie")
async def guest_location_cookie(request: Request) -> dict[str, str | None]:
    return {"token": request.cookies.get(guest_location.GUEST_LOCATION_COOKIE)}


@pytest.fixture(autouse=True)
def clear_session_settings_cache():
    session.get_session_settings.cache_clear()
    yield
    session.get_session_settings.cache_clear()


@pytest.fixture
def stub_session_settings(monkeypatch: pytest.MonkeyPatch):
    settings = SimpleNamespace(
        app_session_secret="test-app-session-secret",
        app_session_ttl_seconds=60 * 60,
    )
    monkeypatch.setattr(session, "get_session_settings", lambda: settings)
    return settings


def test_round_trip_session_token(stub_session_settings) -> None:
    token = session.create_session_token(
        {
            "sub": "user-1",
            "email": "mario@example.com",
            "role": "customer",
            "auth_user_updated_at": "2026-06-15T10:00:00+00:00",
        }
    )

    payload = session.read_session_token(token)

    assert payload is not None
    assert payload["sub"] == "user-1"
    assert payload["email"] == "mario@example.com"
    assert payload["role"] == "customer"
    assert payload["auth_user_updated_at"] == "2026-06-15T10:00:00+00:00"


def test_expired_session_token_is_rejected(stub_session_settings) -> None:
    token = session.create_session_token(
        {
            "sub": "user-1",
            "email": "mario@example.com",
            "role": "customer",
        },
        lifetime_seconds=-1,
    )

    assert session.read_session_token(token) is None


def test_invalid_session_token_is_rejected(stub_session_settings) -> None:
    assert session.read_session_token("not-a-token") is None


def test_guest_location_token_is_signed_and_has_fixed_radius(stub_session_settings) -> None:
    token = create_guest_location_token("015146")

    assert read_guest_location(token) == ("015146", GUEST_LOCATION_RADIUS_KM)
    assert read_guest_location(f"{token}tampered") is None


def test_guest_location_cookie_uses_lax_without_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guest_location.settings, "environment", "development")

    assert guest_location.cookie_secure() is False
    assert guest_location.cookie_samesite() == "lax"


def test_guest_location_cookie_uses_none_with_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guest_location.settings, "environment", "production")

    assert guest_location.cookie_secure() is True
    assert guest_location.cookie_samesite() == "none"


def test_guest_location_cookie_uses_none_for_https_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guest_location.settings, "environment", "development")

    assert guest_location.cookie_secure("https://app.girospesa.local") is True
    assert guest_location.cookie_samesite("https://app.girospesa.local") == "none"


@pytest.mark.asyncio
async def test_guest_location_endpoint_sets_cross_site_cookie_for_capacitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guest_location_router_module,
        "get_municipality",
        lambda *_: {"code": "080061"},
    )
    transport = httpx.ASGITransport(app=guest_location_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
        response = await client.post(
            "/guest-location",
            headers={"Origin": "https://app.girospesa.local"},
            json={"municipality_code": "080061"},
        )

    cookie = response.headers["set-cookie"]
    assert response.status_code == 204
    assert "SameSite=none" in cookie
    assert "Secure" in cookie


@pytest.mark.asyncio
async def test_guest_location_endpoint_rejects_user_coordinates() -> None:
    transport = httpx.ASGITransport(app=guest_location_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
        response = await client.post(
            "/guest-location",
            json={"municipality_code": "080061", "lat": 38.4, "lng": 16.1},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_guest_location_cookie_round_trips_on_direct_api_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guest_location_router_module,
        "get_municipality",
        lambda *_: {"code": "080061"},
    )
    transport = httpx.ASGITransport(app=guest_location_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.girospesa.it",
    ) as client:
        response = await client.post(
            "/guest-location",
            headers={"Origin": "https://www.girospesa.it"},
            json={"municipality_code": "080061"},
        )
        cookie_response = await client.get("/guest-location/cookie")

    assert response.status_code == 204
    assert cookie_response.json()["token"]


def test_session_settings_only_require_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("APP_SESSION_SECRET", "env-session-secret")

    settings = session.get_session_settings()

    assert settings.app_session_secret == "env-session-secret"
