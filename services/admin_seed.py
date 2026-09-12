"""Env-driven Supabase admin seed flow."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from core.config import settings
ADMIN_MUNICIPALITY_CODE = "080061"
DEFAULT_LIST_NAME = "La mia lista"


@dataclass(frozen=True)
class AdminSeed:
    email: str
    password: str
    jwt_role: str
    profile_role: str


@dataclass(frozen=True)
class AdminSeedResult:
    user_id: str
    created_auth_user: bool
    updated_auth_user: bool
    profile_upserted: bool
    default_list_created: bool


@dataclass(frozen=True)
class AdminSeedHealth:
    auth_user_exists: bool
    user_id: str | None
    profile_role: str | None
    login_ok: bool


@dataclass(frozen=True)
class SeededAuthUser:
    user_id: str
    created: bool
    updated: bool


def load_admin_seed_from_env() -> AdminSeed:
    email = os.environ.get("ADMIN_EMAIL", "").strip() or settings.admin_email.strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip() or settings.admin_password.strip()
    if not email:
        raise RuntimeError("ADMIN_EMAIL is required for admin seeding.")
    if not password:
        raise RuntimeError("ADMIN_PASSWORD is required for admin seeding.")
    return AdminSeed(
        email=email,
        password=password,
        jwt_role="admin",
        profile_role="admin",
    )


def seed_admin_user(supabase_client, seed: AdminSeed) -> AdminSeedResult:
    auth_user = _ensure_admin_auth_user(supabase_client, seed)
    _upsert_admin_profile(supabase_client, seed, auth_user.user_id)
    default_list_created = ensure_default_empty_list_for_user(supabase_client, auth_user.user_id)
    return AdminSeedResult(
        user_id=auth_user.user_id,
        created_auth_user=auth_user.created,
        updated_auth_user=auth_user.updated,
        profile_upserted=True,
        default_list_created=default_list_created,
    )


def _ensure_admin_auth_user(supabase_client, seed: AdminSeed) -> SeededAuthUser:
    existing = find_user_by_email(supabase_client.auth.admin, seed.email)
    if existing is None:
        user = supabase_client.auth.admin.create_user(
            {
                "email": seed.email,
                "password": seed.password,
                "email_confirm": True,
                "app_metadata": {"role": seed.jwt_role},
            }
        ).user
        return SeededAuthUser(user_id=user.id, created=True, updated=False)
    app_metadata = getattr(existing, "app_metadata", {}) or {}
    if app_metadata.get("role") == seed.jwt_role:
        return SeededAuthUser(user_id=existing.id, created=False, updated=False)
    supabase_client.auth.admin.update_user_by_id(
        existing.id,
        {"app_metadata": {"role": seed.jwt_role}},
    )
    return SeededAuthUser(user_id=existing.id, created=False, updated=True)


def _upsert_admin_profile(supabase_client, seed: AdminSeed, user_id: str) -> None:
    supabase_client.table("user_profiles").upsert(
        _admin_profile_payload(seed, user_id),
        on_conflict="id",
    ).execute()


def _admin_profile_payload(seed: AdminSeed, user_id: str) -> dict:
    return {
        "id": user_id,
        "display_name": _display_name_from_email(seed.email),
        "municipality_code": ADMIN_MUNICIPALITY_CODE,
        "role": seed.profile_role,
        "managed_supermarket_id": None,
    }


def ensure_default_empty_list_for_user(supabase_client, user_id: str) -> bool:
    list_id = _find_owned_list_id_for_user(supabase_client, user_id)
    if list_id is not None:
        _ensure_owner_membership(supabase_client, list_id, user_id)
        return False
    list_id = _create_default_empty_list(supabase_client, user_id)
    _ensure_owner_membership(supabase_client, list_id, user_id)
    return True


def _find_owned_list_id_for_user(supabase_client, user_id: str) -> str | None:
    result = (
        supabase_client.table("shopping_lists")
        .select("id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return None if not rows else rows[0]["id"]


def _create_default_empty_list(supabase_client, user_id: str) -> str:
    result = supabase_client.table("shopping_lists").insert(
        {"user_id": user_id, "name": DEFAULT_LIST_NAME, "items": [], "is_active": True}
    ).execute()
    rows = result.data or []
    if not rows:
        raise RuntimeError("Default shopping list creation returned no row.")
    return rows[0]["id"]


def _ensure_owner_membership(supabase_client, list_id: str, user_id: str) -> None:
    supabase_client.table("list_members").upsert(
        {"list_id": list_id, "user_id": user_id, "role": "owner"},
        on_conflict="list_id,user_id",
    ).execute()


def check_admin_seed_health(supabase_client, seed: AdminSeed) -> AdminSeedHealth:
    user = find_user_by_email(supabase_client.auth.admin, seed.email)
    profile_role = None
    if user is not None:
        result = (
            supabase_client.table("user_profiles")
            .select("role")
            .eq("id", user.id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            profile_role = rows[0]["role"]
    return AdminSeedHealth(
        auth_user_exists=user is not None,
        user_id=None if user is None else user.id,
        profile_role=profile_role,
        login_ok=_login_smoke_check(seed),
    )


def find_user_by_email(admin_api, email: str):
    page = 1
    per_page = 200
    while True:
        response = admin_api.list_users(page=page, per_page=per_page)
        users = getattr(response, "users", response)
        if isinstance(users, dict):
            users = users.get("users", [])
        if not users:
            return None
        for user in users:
            if getattr(user, "email", None) == email:
                return user
        if len(users) < per_page:
            return None
        page += 1


def _login_smoke_check(seed: AdminSeed) -> bool:
    supabase_url = _required_env("SUPABASE_URL", settings.supabase_url)
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "apikey": _resolve_anon_key(),
    }
    response = httpx.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers=headers,
        json={"email": seed.email, "password": seed.password, "gotrue_meta_security": {}},
        timeout=5,
    )
    return response.status_code == 200


def _resolve_anon_key() -> str:
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if anon_key:
        return anon_key
    if settings.supabase_url:
        status_env = _read_supabase_status_env()
    else:
        status_env = ""
    publishable_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if publishable_key:
        return publishable_key
    for key_name in ("ANON_KEY", "PUBLISHABLE_KEY"):
        match = re.search(rf'^{key_name}="([^"]+)"$', status_env, re.MULTILINE)
        if match is not None:
            return match.group(1)
    raise RuntimeError("SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY is required for login smoke check.")


def _read_supabase_status_env() -> str:
    return subprocess.check_output(
        ["supabase", "status", "-o", "env"],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
    )


def _display_name_from_email(email: str) -> str:
    return email.split("@", 1)[0]


def _required_env(name: str, fallback: str = "") -> str:
    value = os.environ.get(name, "").strip() or fallback.strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def _run_local_psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            _resolve_db_container_name(),
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-tA",
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        input=sql,
        text=True,
        capture_output=True,
    )


def _resolve_db_container_name() -> str:
    container_name = os.environ.get("SUPABASE_DB_CONTAINER", "").strip()
    if container_name:
        return container_name
    config_toml = (Path(__file__).resolve().parents[1] / "supabase" / "config.toml").read_text(
        encoding="utf-8"
    )
    project_id_match = re.search(r'project_id = "([^"]+)"', config_toml)
    if project_id_match is None:
        raise RuntimeError("Cannot resolve Supabase project_id from supabase/config.toml.")
    return f"supabase_db_{project_id_match.group(1)}"
