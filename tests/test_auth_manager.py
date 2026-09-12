"""Unit tests for auth dependencies in core/auth.py."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock(  # type: ignore[attr-defined]
    supabase_url="https://project.supabase.co",
)
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

if "core.auth" in sys.modules:
    del sys.modules["core.auth"]

import core.auth as _auth_module

import pytest
from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


class TestRequireAdminOrManager:
    def test_accepts_admin(self):
        profile = {"id": "u1", "role": "admin", "managed_supermarket_id": None}
        result = _run(_auth_module.require_admin_or_manager(profile))
        assert result == profile

    def test_accepts_supermarket_manager(self):
        profile = {"id": "u2", "role": "supermarket_manager", "managed_supermarket_id": "sup-1"}
        result = _run(_auth_module.require_admin_or_manager(profile))
        assert result == profile

    def test_rejects_customer_with_403(self):
        profile = {"id": "u3", "role": "customer", "managed_supermarket_id": None}
        with pytest.raises(HTTPException) as exc_info:
            _run(_auth_module.require_admin_or_manager(profile))
        assert exc_info.value.status_code == 403


class TestDecodeToken:
    def test_rejects_hs256_tokens_without_shared_secret(self, monkeypatch: pytest.MonkeyPatch):
        header = MagicMock(return_value={"alg": "HS256"})
        monkeypatch.setattr(_auth_module.jwt, "get_unverified_header", header)
        _auth_module.settings.supabase_jwt_secret = ""

        with pytest.raises(HTTPException) as exc_info:
            _auth_module._decode_token("token")

        assert exc_info.value.status_code == 401

    def test_uses_shared_secret_for_hs256(self, monkeypatch: pytest.MonkeyPatch):
        decode = MagicMock(return_value={"sub": "local-user"})
        header = MagicMock(return_value={"alg": "HS256"})
        monkeypatch.setattr(_auth_module.jwt, "decode", decode)
        monkeypatch.setattr(_auth_module.jwt, "get_unverified_header", header)
        _auth_module.settings.supabase_jwt_secret = "local-jwt-secret"

        result = _auth_module._decode_token("token")

        assert result == {"sub": "local-user"}
        decode.assert_called_once_with(
            "token",
            "local-jwt-secret",
            algorithms=["HS256"],
            audience="authenticated",
            issuer="https://project.supabase.co/auth/v1",
        )

    def test_uses_jwks_for_es256(self, monkeypatch: pytest.MonkeyPatch):
        decode = MagicMock(return_value={"sub": "u2"})
        header = MagicMock(return_value={"alg": "ES256"})
        monkeypatch.setattr(_auth_module.jwt, "decode", decode)
        monkeypatch.setattr(_auth_module.jwt, "get_unverified_header", header)
        monkeypatch.setattr(_auth_module, "_load_jwks", lambda: {"keys": [{"kid": "k1"}]})

        result = _auth_module._decode_token("token")

        assert result == {"sub": "u2"}
        decode.assert_called_once_with(
            "token",
            {"keys": [{"kid": "k1"}]},
            algorithms=["ES256"],
            audience="authenticated",
            issuer="https://project.supabase.co/auth/v1",
        )

    def test_uses_jwks_for_rs256(self, monkeypatch: pytest.MonkeyPatch):
        decode = MagicMock(return_value={"sub": "u3"})
        header = MagicMock(return_value={"alg": "RS256"})
        monkeypatch.setattr(_auth_module.jwt, "decode", decode)
        monkeypatch.setattr(_auth_module.jwt, "get_unverified_header", header)
        monkeypatch.setattr(_auth_module, "_load_jwks", lambda: {"keys": [{"kid": "k2"}]})

        result = _auth_module._decode_token("token")

        assert result == {"sub": "u3"}
        decode.assert_called_once_with(
            "token",
            {"keys": [{"kid": "k2"}]},
            algorithms=["RS256"],
            audience="authenticated",
            issuer="https://project.supabase.co/auth/v1",
        )


class TestAssertFlyerAccess:
    def test_admin_unrestricted(self):
        profile = {"role": "admin", "managed_supermarket_id": None}
        flyer = {"supermarket_id": "any-supermarket"}
        _auth_module.assert_flyer_access(profile, flyer)

    def test_manager_own_supermarket_ok(self):
        profile = {"role": "supermarket_manager", "managed_supermarket_id": "sup-1"}
        flyer = {"supermarket_id": "sup-1"}
        _auth_module.assert_flyer_access(profile, flyer)

    def test_manager_wrong_supermarket_raises_403(self):
        profile = {"role": "supermarket_manager", "managed_supermarket_id": "sup-1"}
        flyer = {"supermarket_id": "sup-other"}
        with pytest.raises(HTTPException) as exc_info:
            _auth_module.assert_flyer_access(profile, flyer)
        assert exc_info.value.status_code == 403


class TestBearerAuth:
    def test_bearer_authenticates_user(self, monkeypatch):
        decode = MagicMock(return_value={"sub": "bearer-user"})
        header = MagicMock(return_value={"alg": "ES256"})
        monkeypatch.setattr(_auth_module.jwt, "decode", decode)
        monkeypatch.setattr(_auth_module.jwt, "get_unverified_header", header)
        monkeypatch.setattr(_auth_module, "_load_jwks", lambda: {"keys": [{"kid": "k1"}]})

        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/protected")
        async def protected(user_id: str = Depends(_auth_module.get_current_user_id)):
            return {"user_id": user_id}

        client = TestClient(app)
        response = client.get("/protected", headers={"Authorization": "Bearer test-token"})

        assert response.status_code == 200
        assert response.json() == {"user_id": "bearer-user"}

    def test_missing_bearer_is_unauthorized(self):
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/protected")
        async def protected(user_id: str = Depends(_auth_module.get_current_user_id)):
            return {"user_id": user_id}

        client = TestClient(app)
        response = client.get("/protected")

        assert response.status_code == 401
