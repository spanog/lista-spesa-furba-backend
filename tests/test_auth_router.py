"""Unit tests for backend auth router — signup and password recovery."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock(
    environment="test",
    frontend_url="http://localhost:3000",
    backend_url="http://localhost:8000",
    supabase_url="http://127.0.0.1:54321",
    supabase_secret_key="sb_secret_test",
)
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

_session_mod = types.ModuleType("core.session")
_session_mod.create_session_token = MagicMock(return_value="test-recovery-token")  # type: ignore[attr-defined]
_session_mod.read_session_token = MagicMock(return_value=None)  # type: ignore[attr-defined]
sys.modules["core.session"] = _session_mod

if "api.routers.auth" in sys.modules:
    del sys.modules["api.routers.auth"]

import api.routers.auth as _auth_router

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def app():
    _app = FastAPI()
    _app.include_router(_auth_router.router)
    return _app


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=True)


_SIGNUP_BODY = {
    "first_name": "Mario",
    "last_name": "Rossi",
    "email": "mario@example.com",
    "password": "Password123!",
    "municipality_code": "015146",
}


def test_signup_calls_backend_only_flow(client, monkeypatch):
    signup_calls: list[object] = []
    monkeypatch.setattr(
        _auth_router,
        "signup_user",
        lambda body: signup_calls.append(body),
    )

    response = client.post("/auth/signup", json=_SIGNUP_BODY)

    assert response.status_code == 201
    assert len(signup_calls) == 1


def test_signup_rejects_legacy_address_or_coordinate_fields(client):
    response = client.post(
        "/auth/signup",
        json={**_SIGNUP_BODY, "home_address": "Via Roma 1", "lat": 45.46},
    )

    assert response.status_code == 422


def test_signup_returns_duplicate_email_detail(client, monkeypatch):
    fake_sb = MagicMock()
    fake_sb.auth.sign_up.side_effect = Exception("User already registered")
    monkeypatch.setattr(_auth_router, "get_supabase", lambda: fake_sb)

    response = client.post("/auth/signup", json=_SIGNUP_BODY)

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Registrazione non riuscita. Verifica i dati inseriti oppure accedi se hai già un account.",
    }


def test_signup_returns_upstream_error_detail(client, monkeypatch):
    fake_sb = MagicMock()
    fake_sb.auth.sign_up.side_effect = Exception("Weak password")
    monkeypatch.setattr(_auth_router, "get_supabase", lambda: fake_sb)

    response = client.post("/auth/signup", json=_SIGNUP_BODY)

    assert response.status_code == 400
    assert response.json() == {"detail": "Password non valida"}


def test_signup_persists_selected_municipality_before_email_confirmation(monkeypatch):
    fake_sb = MagicMock()
    fake_sb.auth.sign_up.return_value.user.id = "user-1"
    fake_sb.auth.sign_up.return_value.user.identities = [MagicMock()]
    monkeypatch.setattr(_auth_router, "get_supabase", lambda: fake_sb)
    monkeypatch.setattr(_auth_router, "get_municipality", lambda *_: {"code": "015146"})

    _auth_router.signup_user(_auth_router.SignupBody(**_SIGNUP_BODY))

    signup_data = fake_sb.auth.sign_up.call_args.args[0]["options"]["data"]
    assert signup_data["display_name"] == "Mario Rossi"
    fake_sb.table.assert_called_once_with("user_profiles")
    fake_sb.table.return_value.update.assert_called_once_with({
        "municipality_code": "015146",
    })
    fake_sb.table.return_value.update.return_value.eq.assert_called_once_with("id", "user-1")


def test_signup_rejects_a_municipality_not_in_the_catalogue(monkeypatch):
    fake_sb = MagicMock()
    monkeypatch.setattr(_auth_router, "get_supabase", lambda: fake_sb)
    monkeypatch.setattr(
        _auth_router,
        "get_municipality",
        MagicMock(side_effect=_auth_router.MunicipalityNotFoundError("bad")),
    )

    with pytest.raises(_auth_router.HTTPException) as error:
        _auth_router.signup_user(_auth_router.SignupBody(**_SIGNUP_BODY))

    assert error.value.status_code == 422
    fake_sb.auth.sign_up.assert_not_called()


def test_signup_does_not_update_a_masked_duplicate_account(monkeypatch):
    fake_sb = MagicMock()
    fake_sb.auth.sign_up.return_value.user.id = "masked-user"
    fake_sb.auth.sign_up.return_value.user.identities = []
    monkeypatch.setattr(_auth_router, "get_supabase", lambda: fake_sb)
    municipality = MagicMock(return_value={"code": "015146"})
    monkeypatch.setattr(_auth_router, "get_municipality", municipality)

    _auth_router.signup_user(_auth_router.SignupBody(**_SIGNUP_BODY))

    municipality.assert_called_once_with(fake_sb, "015146")
    fake_sb.table.assert_not_called()


def test_forgot_password_uses_secret_key_client(client, monkeypatch):
    fake_sb = MagicMock()
    monkeypatch.setattr(_auth_router, "_fresh_supabase_client", lambda: fake_sb)

    response = client.post("/auth/forgot-password", json={"email": "mario@example.com"})

    assert response.status_code == 204
    fake_sb.auth.reset_password_email.assert_called_once_with(
        "mario@example.com",
        {"redirect_to": "http://localhost:8000/auth/callback"},
    )


def test_reset_password_requires_backend_recovery_token(client):
    _session_mod.read_session_token.return_value = None

    response = client.post("/auth/reset-password", json={
        "recovery_token": "bad-token",
        "password": "Password123!",
    })

    assert response.status_code == 400


def test_auth_callback_rejects_absolute_next_redirects(client, monkeypatch):
    fake_sb = MagicMock()
    fake_sb.auth.verify_otp.return_value.user = MagicMock(
        id="user-1",
        updated_at="2026-06-15T10:00:00+00:00",
    )
    monkeypatch.setattr(_auth_router, "_fresh_supabase_client", lambda: fake_sb)

    response = client.get(
        "/auth/callback",
        params={
            "token_hash": "valid",
            "type": "recovery",
            "next": "https://evil.example/steal",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "http://localhost:3000/reset-password?token="
    )


def test_reset_password_rejects_replayed_backend_recovery_token(client, monkeypatch):
    _session_mod.read_session_token.return_value = {
        "sub": "user-1",
        "purpose": "password_reset",
        "auth_user_updated_at": "2026-06-15T10:00:00+00:00",
    }
    fake_sb = MagicMock()
    fake_sb.auth.admin.get_user_by_id.return_value.user = MagicMock(
        updated_at="2026-06-15T10:00:01+00:00"
    )
    monkeypatch.setattr(_auth_router, "_fresh_supabase_client", lambda: fake_sb)

    response = client.post(
        "/auth/reset-password",
        json={"recovery_token": "token", "password": "Password123!"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid or expired recovery token"}
