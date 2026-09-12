"""Unit tests for env-driven admin seed flow."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("SUPABASE_SECRET_KEY", "test-secret-key")

if "core.config" in sys.modules and not hasattr(sys.modules["core.config"], "Settings"):
    sys.modules.pop("core.config")

from services import admin_seed


class _FakeUser:
    def __init__(self, user_id: str, email: str, app_metadata: dict | None = None):
        self.id = user_id
        self.email = email
        self.app_metadata = app_metadata or {}


class _FakeUserResponse:
    def __init__(self, user: _FakeUser):
        self.user = user


class _FakeAdminApi:
    def __init__(self, users: list[_FakeUser] | None = None):
        self.users = users or []
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []

    def list_users(self, page: int | None = None, per_page: int | None = None):
        return list(self.users)

    def create_user(self, attributes: dict):
        self.create_calls.append(attributes)
        user = _FakeUser("created-admin-id", attributes["email"], attributes.get("app_metadata"))
        self.users.append(user)
        return _FakeUserResponse(user)

    def update_user_by_id(self, user_id: str, attributes: dict):
        self.update_calls.append((user_id, attributes))
        current = next(user for user in self.users if user.id == user_id)
        current.app_metadata = attributes["app_metadata"]
        return _FakeUserResponse(current)


class _FakeAuth:
    def __init__(self, admin_api: _FakeAdminApi):
        self.admin = admin_api


class _FakeTable:
    def __init__(
        self,
        select_data: list[dict] | None = None,
        insert_data: list[dict] | None = None,
    ):
        self.select_data = select_data or []
        self.insert_data = insert_data or []
        self.upsert_calls: list[tuple[dict, str]] = []
        self.insert_calls: list[dict] = []
        self.select_calls: list[str] = []
        self.eq_calls: list[tuple[str, object]] = []
        self.limit_calls: list[int] = []
        self._last_operation = ""

    def upsert(self, payload: dict, on_conflict: str):
        self.upsert_calls.append((payload, on_conflict))
        return self

    def insert(self, payload: dict):
        self.insert_calls.append(payload)
        self._last_operation = "insert"
        return self

    def select(self, columns: str):
        self.select_calls.append(columns)
        self._last_operation = "select"
        return self

    def eq(self, column: str, value: object):
        self.eq_calls.append((column, value))
        return self

    def limit(self, count: int):
        self.limit_calls.append(count)
        return self

    def execute(self):
        data = self.insert_data if self._last_operation == "insert" else self.select_data
        return type("_Result", (), {"data": data})()


class _FakeSupabase:
    def __init__(self, users: list[_FakeUser] | None = None):
        self.auth = _FakeAuth(_FakeAdminApi(users))
        self.user_profiles = _FakeTable()
        self.shopping_lists = _FakeTable(insert_data=[{"id": "created-list-id"}])
        self.list_members = _FakeTable()

    def table(self, name: str):
        tables = {
            "user_profiles": self.user_profiles,
            "shopping_lists": self.shopping_lists,
            "list_members": self.list_members,
        }
        assert name in tables
        return tables[name]


def test_load_admin_seed_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-123")

    seed = admin_seed.load_admin_seed_from_env()

    assert seed.email == "admin@example.com"
    assert seed.password == "pw-123"
    assert seed.jwt_role == "admin"
    assert seed.profile_role == "admin"


def test_load_admin_seed_requires_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(admin_seed.settings, "admin_email", "")
    monkeypatch.setattr(admin_seed.settings, "admin_password", "")

    with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
        admin_seed.load_admin_seed_from_env()


def test_seed_admin_creates_missing_admin_user(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-123")
    supabase = _FakeSupabase()

    result = admin_seed.seed_admin_user(supabase, admin_seed.load_admin_seed_from_env())

    assert result.created_auth_user is True
    assert result.updated_auth_user is False
    assert supabase.auth.admin.create_calls == [
        {
            "email": "admin@example.com",
            "password": "pw-123",
            "email_confirm": True,
            "app_metadata": {"role": "admin"},
        }
    ]
    assert supabase.user_profiles.upsert_calls == [
        (
            {
                "id": "created-admin-id",
                "display_name": "admin",
                "municipality_code": "080061",
                "role": "admin",
                "managed_supermarket_id": None,
            },
            "id",
        )
    ]
    assert result.default_list_created is True
    assert supabase.shopping_lists.insert_calls == [
        {
            "user_id": "created-admin-id",
            "name": "La mia lista",
            "items": [],
            "is_active": True,
        }
    ]
    assert supabase.list_members.upsert_calls == [
        (
            {
                "list_id": "created-list-id",
                "user_id": "created-admin-id",
                "role": "owner",
            },
            "list_id,user_id",
        )
    ]


def test_seed_admin_skips_existing_admin_but_keeps_profile_in_sync(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-123")
    existing = _FakeUser("existing-admin-id", "admin@example.com", {"role": "admin"})
    supabase = _FakeSupabase([existing])

    result = admin_seed.seed_admin_user(supabase, admin_seed.load_admin_seed_from_env())

    assert result.created_auth_user is False
    assert result.updated_auth_user is False
    assert supabase.auth.admin.create_calls == []
    assert supabase.auth.admin.update_calls == []
    assert supabase.user_profiles.upsert_calls[0][0]["id"] == "existing-admin-id"


def test_seed_admin_does_not_duplicate_existing_active_empty_list(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-123")
    existing = _FakeUser("existing-admin-id", "admin@example.com", {"role": "admin"})
    supabase = _FakeSupabase([existing])
    supabase.shopping_lists.select_data = [{"id": "existing-list-id"}]

    result = admin_seed.seed_admin_user(supabase, admin_seed.load_admin_seed_from_env())

    assert result.default_list_created is False
    assert supabase.shopping_lists.insert_calls == []
    assert supabase.list_members.upsert_calls == [
        (
            {
                "list_id": "existing-list-id",
                "user_id": "existing-admin-id",
                "role": "owner",
            },
            "list_id,user_id",
        )
    ]


def test_seed_admin_updates_existing_user_missing_admin_role(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-123")
    existing = _FakeUser("existing-admin-id", "admin@example.com", {"role": "customer"})
    supabase = _FakeSupabase([existing])

    result = admin_seed.seed_admin_user(supabase, admin_seed.load_admin_seed_from_env())

    assert result.created_auth_user is False
    assert result.updated_auth_user is True
    assert supabase.auth.admin.update_calls == [
        ("existing-admin-id", {"app_metadata": {"role": "admin"}})
    ]


def test_find_user_by_email_returns_match():
    admin_api = _FakeAdminApi(
        [_FakeUser("u1", "one@example.com"), _FakeUser("u2", "two@example.com")]
    )

    user = admin_seed.find_user_by_email(admin_api, "two@example.com")

    assert user is not None
    assert user.id == "u2"


def test_module_uses_runtime_env_not_hardcoded_local_seed():
    assert "LOCAL_ADMIN_PASSWORD" not in admin_seed.__dict__
    assert os.path.basename(admin_seed.__file__) == "admin_seed.py"


def test_runtime_config_has_no_geocoding_provider():
    assert "geocoding_provider" not in type(admin_seed.settings).model_fields
