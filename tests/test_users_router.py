"""Unit tests for api/routers/users.py — body validation and business rules.

These tests cover only the Pydantic request models (pure domain logic).
Infrastructure modules (supabase, jose, etc.) are stubbed so the tests run
without a venv or external services.
"""

import sys
import os
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# Stub out infrastructure modules that are not available in the system Python
# ---------------------------------------------------------------------------
for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Stub core.config so Settings() doesn't require env vars
_config_mod = types.ModuleType("core.config")
_settings_stub = MagicMock()
_config_mod.settings = _settings_stub  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod

# Stub core.database and core.auth (no DB/JWT calls in these tests)
sys.modules["core.database"] = MagicMock()
sys.modules["core.auth"] = MagicMock()

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routers.users import UpdateProfileBody


@pytest.fixture(scope="module", autouse=True)
def cleanup_stubbed_modules():
    yield
    for name in (
        "api",
        "api.routers",
        "api.routers.users",
        "core.auth",
        "core.config",
        "core.database",
    ):
        sys.modules.pop(name, None)


class TestUpdateProfileBody:
    """Validate that UpdateProfileBody accepts and rejects the right inputs."""

    def test_all_none_is_valid(self):
        """An empty patch (all fields None) is valid — partial updates are allowed."""
        body = UpdateProfileBody()
        assert body.model_dump(exclude_none=True) == {}

    def test_display_name_only(self):
        body = UpdateProfileBody(display_name="Mario Rossi")
        dumped = body.model_dump(exclude_none=True)
        assert dumped == {"display_name": "Mario Rossi"}

    def test_preferred_supermarkets_are_rejected(self):
        with pytest.raises(ValidationError):
            UpdateProfileBody(preferred_supermarkets=["coop"])

    def test_municipality_code_is_accepted(self):
        body = UpdateProfileBody(municipality_code="080061")
        dumped = body.model_dump(exclude_none=True)
        assert dumped == {"municipality_code": "080061"}

    def test_non_istat_municipality_code_is_rejected(self):
        with pytest.raises(ValidationError):
            UpdateProfileBody(municipality_code="Milano")

    def test_max_distance_km_lower_bound(self):
        body = UpdateProfileBody(max_distance_km=1)
        assert body.max_distance_km == 1

    def test_max_distance_km_upper_bound(self):
        body = UpdateProfileBody(max_distance_km=20)
        assert body.max_distance_km == 20

    def test_max_distance_km_below_min_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            UpdateProfileBody(max_distance_km=0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("max_distance_km",) for e in errors)

    def test_max_distance_km_above_max_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            UpdateProfileBody(max_distance_km=21)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("max_distance_km",) for e in errors)

    def test_notifications_enabled_flag(self):
        body = UpdateProfileBody(notifications_enabled=False)
        dumped = body.model_dump(exclude_none=True)
        assert dumped["notifications_enabled"] is False

    def test_address_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            UpdateProfileBody(home_address="Via Roma 1")

    def test_model_dump_exclude_none_omits_unset_fields(self):
        """Partial patch: only the specified field appears in the dump."""
        body = UpdateProfileBody(display_name="Test")
        dumped = body.model_dump(exclude_none=True)
        assert list(dumped.keys()) == ["display_name"]


class TestDeleteAccount:
    def test_cleanup_dependencies_rewrites_invite_refs(self, monkeypatch):
        sb = MagicMock()

        from api.routers import users

        users._cleanup_account_delete_dependencies(sb, "user-1")

        assert sb.table.call_args_list[0].args == ("list_members",)
        assert (
            sb.table.return_value.update.return_value.eq.return_value.execute.called
        )

    @pytest.mark.asyncio
    async def test_returns_204(self, monkeypatch):
        from api.routers import users
        from api.routers.users import delete_account

        monkeypatch.setattr(users, "_delete_auth_user", MagicMock())

        result = await delete_account("user-1")

        assert result.status_code == 204

    def test_maps_backend_delete_error_to_http_502(self, monkeypatch):
        sb = MagicMock()
        sb.auth.admin.delete_user.side_effect = RuntimeError("backend boom")

        from api.routers import users

        monkeypatch.setattr(users, "get_supabase", MagicMock(return_value=sb))
        monkeypatch.setattr(users, "_cleanup_account_delete_dependencies", MagicMock())

        with pytest.raises(HTTPException) as exc_info:
            users._delete_auth_user("user-1")

        assert exc_info.value.status_code == 502
        assert "Eliminazione account" in exc_info.value.detail

    def test_missing_user_is_treated_as_success(self, monkeypatch):
        sb = MagicMock()
        sb.auth.admin.delete_user.side_effect = RuntimeError("User not found")

        from api.routers import users

        monkeypatch.setattr(users, "get_supabase", MagicMock(return_value=sb))
        monkeypatch.setattr(users, "_cleanup_account_delete_dependencies", MagicMock())

        users._delete_auth_user("user-1")
