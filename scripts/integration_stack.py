"""Manage isolated Docker Compose stack for integration tests."""

from __future__ import annotations

import argparse
import os
import string
import subprocess
import time
from pathlib import Path

from jose import jwt as _jwt


BACKEND_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = BACKEND_ROOT / "docker-compose.integration.yml"
KONG_TEMPLATE = BACKEND_ROOT / "supabase" / "kong.integration.yml.tmpl"
KONG_CONFIG = BACKEND_ROOT / "supabase" / "kong.integration.yml"
PROJECT_NAME = "girospesa-itest"
JWT_SECRET = "integration-test-jwt-secret-with-at-least-32-chars"
SUPABASE_URL = "http://127.0.0.1:55421"
DB_DSN = "postgresql://postgres:postgres@127.0.0.1:55422/postgres"

_JWT_BASE_PAYLOAD = {
    "iss": "supabase",
    "ref": "girospesa-itest",
    "iat": 1714000000,
    "exp": 2059576000,
}


def _make_jwt(role: str) -> str:
    return _jwt.encode({**_JWT_BASE_PAYLOAD, "role": role}, JWT_SECRET, algorithm="HS256")


def _anon_key() -> str:
    return _make_jwt("anon")


def _service_role_key() -> str:
    return _make_jwt("service_role")


def _generate_kong_config() -> None:
    template = string.Template(KONG_TEMPLATE.read_text())
    KONG_CONFIG.write_text(
        template.substitute(ANON_KEY=_anon_key(), SERVICE_ROLE_KEY=_service_role_key())
    )


def integration_env() -> dict[str, str]:
    return {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_ANON_KEY": _anon_key(),
        "SUPABASE_SECRET_KEY": _service_role_key(),
        "SUPABASE_INTERNAL_JWT_SECRET": JWT_SECRET,
        "SUPABASE_DB_CONTAINER": f"{PROJECT_NAME}-db-1",
        "DB_DSN": DB_DSN,
        "ADMIN_EMAIL": "test-admin@local.test",
        "ADMIN_PASSWORD": "TestAdmin123!",
        "LLM_PROVIDER": "gemini",
        "GOOGLE_API_KEY": "",
        "GEMINI_MODEL": "gemini-2.5-flash",
        "FRONTEND_URL": "http://127.0.0.1:3000",
    }


def apply_integration_env() -> None:
    os.environ.update(integration_env())


def compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        PROJECT_NAME,
        *args,
    ]


def _wait_timeout_seconds() -> str:
    return os.environ.get("INTEGRATION_COMPOSE_WAIT_TIMEOUT_SECONDS", "180")


def _with_wait_timeout(args: tuple[str, ...]) -> tuple[str, ...]:
    if not args or args[0] != "up" or "--wait" not in args or "--wait-timeout" in args:
        return args
    if len(args) > 3 and not args[-1].startswith("-"):
        return (*args[:-1], "--wait-timeout", _wait_timeout_seconds(), args[-1])
    return (*args, "--wait-timeout", _wait_timeout_seconds())


def _run_capture(*args: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        compose_command(*args),
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _print_diagnostic(label: str, *args: str, env: dict[str, str]) -> None:
    print(f"\n--- docker compose {label} ---", flush=True)
    subprocess.run(compose_command(*args), cwd=BACKEND_ROOT, env=env, check=False)


def _dump_compose_diagnostics(env: dict[str, str]) -> None:
    _print_diagnostic("ps -a", "ps", "-a", env=env)
    _print_diagnostic(
        "logs --no-color --tail=200",
        "logs",
        "--no-color",
        "--tail=200",
        env=env,
    )


def _run_compose_checked(*args: str, env: dict[str, str]) -> None:
    final_args = _with_wait_timeout(args)
    try:
        subprocess.run(compose_command(*final_args), cwd=BACKEND_ROOT, env=env, check=True)
    except subprocess.CalledProcessError:
        if args and args[0] == "up":
            _dump_compose_diagnostics(env)
        raise


def _wait_for_schema(env: dict[str, str], timeout_seconds: int = 60) -> None:
    sql = """
    SELECT
      EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'shopping_lists'
          AND column_name = 'name'
      )
      AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'user_profiles'
          AND column_name = 'notifications_enabled'
      )
      AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'list_invites'
          AND column_name = 'invited_user_id'
      )
      AND to_regclass('public.app_notifications') IS NOT NULL
      AND to_regprocedure('public.append_list_item(uuid, jsonb)') IS NOT NULL;
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        output = _run_capture(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-tAc",
            sql,
            env=env,
        )
        if output.lower() == "t":
            _run_capture(
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-c",
                "NOTIFY pgrst, 'reload schema';",
                env=env,
            )
            return
        time.sleep(1)
    raise RuntimeError("Timed out waiting for integration schema bootstrap")


def run_compose(*args: str) -> None:
    if args and args[0] == "up":
        _generate_kong_config()
    env = os.environ.copy()
    env.update(integration_env())
    _run_compose_checked(*args, env=env)
    if args and args[0] == "up":
        _wait_for_schema(env)
        # PostgREST may start before local bootstrap migrations finish. Restart it
        # only after schema objects exist so cache sees new columns/RPCs/tables.
        _run_compose_checked("restart", "rest", env=env)
        _run_compose_checked("up", "-d", "--wait", "rest", env=env)


def print_env() -> None:
    for key, value in integration_env().items():
        print(f"{key}={value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["up", "down", "status", "env"])
    args = parser.parse_args()

    if args.command == "up":
        _generate_kong_config()
        run_compose("up", "-d", "--wait")
    elif args.command == "down":
        run_compose("down", "-v", "--remove-orphans")
    elif args.command == "status":
        run_compose("ps")
    else:
        print_env()


if __name__ == "__main__":
    main()
