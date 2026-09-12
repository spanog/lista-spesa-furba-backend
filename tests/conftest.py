"""Fixtures condivise per i test di integrazione.

I test unitari (tests/test_*.py) NON usano queste fixture — continuano ad usare
mock/stub come prima. Le fixture qui si attivano solo quando il test usa
esplicitamente `supabase_client` o `async_client`.
"""

import os
import sys
import inspect
import time
import types
from pathlib import Path

import pytest
import httpx
import psycopg2
from dotenv import load_dotenv

from tests.env import resolve_test_env_file


BACKEND_ROOT = Path(__file__).resolve().parents[1]


load_dotenv(resolve_test_env_file(BACKEND_ROOT), override=False)


def _resolve_local_supabase_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value and not value.startswith("<local-"):
        return value
    raise RuntimeError(f"Missing test Supabase value for {name}.")


def wait_for_user_bootstrap(user_id: str, *, timeout_seconds: float = 5.0) -> None:
    """Wait until auth.users and user_profiles both expose the new user."""
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM auth.users WHERE id = %s LIMIT 1", (user_id,))
        auth_ready = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM public.user_profiles WHERE id = %s LIMIT 1", (user_id,))
        profile_ready = cur.fetchone() is not None
        conn.close()
        if auth_ready and profile_ready:
            return
        time.sleep(0.1)
    raise RuntimeError(f"User bootstrap not ready for {user_id}")


if "app" not in inspect.signature(httpx.AsyncClient.__init__).parameters:
    _HttpxAsyncClient = httpx.AsyncClient

    class CompatAsyncClient(_HttpxAsyncClient):
        def __init__(self, *args, app=None, transport=None, **kwargs):
            if app is not None and transport is None:
                transport = httpx.ASGITransport(app=app)
            super().__init__(*args, transport=transport, **kwargs)

    httpx.AsyncClient = CompatAsyncClient

# ---------------------------------------------------------------------------
# Guard: verifica Supabase test prima di qualsiasi test di integrazione
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=False)
def ensure_supabase_local():
    """Verifica che lo stack Supabase configurato per i test sia raggiungibile.

    Non è autouse=True per non bloccare i test unitari che non ne hanno bisogno.
    I test di integrazione devono richiedere questa fixture esplicitamente o
    includerla via conftest di sotto-directory.
    """
    url = _resolve_local_supabase_env("SUPABASE_URL") + "/rest/v1/"
    anon_key = _resolve_local_supabase_env("SUPABASE_ANON_KEY")
    try:
        r = httpx.get(url, headers={"apikey": anon_key}, timeout=3)
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(
            f"Supabase test non raggiungibile: {exc}\n"
            "→ Esegui: .venv/bin/python -m scripts.integration_stack up",
            returncode=1,
        )


# ---------------------------------------------------------------------------
# Supabase client con secret key (bypass RLS) — per setup/teardown dati
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def supabase_client(ensure_supabase_local):
    """Client Supabase con secret key — bypassa RLS per seed e cleanup."""
    maybe_mocked = sys.modules.get("supabase")
    if maybe_mocked is not None and getattr(maybe_mocked, "__file__", None) is None:
        sys.modules.pop("supabase", None)

    from supabase import create_client

    url = _resolve_local_supabase_env("SUPABASE_URL")
    key = _resolve_local_supabase_env("SUPABASE_SECRET_KEY")
    return create_client(url, key)


# ---------------------------------------------------------------------------
# AsyncClient per chiamate HTTP a FastAPI in-process
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
async def async_client(ensure_supabase_local):
    """AsyncClient HTTPX che punta a FastAPI in-process (no rete esterna)."""
    from main import app  # importa l'app FastAPI del backend

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Teardown: truncate tabelle di test dopo ogni test di integrazione
# ---------------------------------------------------------------------------

TRUNCATE_TABLES = [
    "list_members",
    "list_invites",
    "shopping_lists",
    "favorites",
    "notification_jobs",
    "offers",
    "products",
    "flyers",
    "supermarkets",
    "extraction_log",
]


_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _delete_all_tables(supabase_client) -> None:
    """Elimina tutte le righe dalle tabelle di test, rispettando l'ordine FK."""
    for table in TRUNCATE_TABLES:
        try:
            supabase_client.table(table).delete().neq("id", _NIL_UUID).execute()
        except Exception:
            pass


@pytest.fixture()
def clean_db(supabase_client):
    """Garantisce un DB pulito prima e dopo ogni test.

    Pulisce anche prima del test per resistere a dati lasciati da sessioni
    precedenti (es. dopo crash o cleanup mancato).
    """
    _delete_all_tables(supabase_client)
    yield
    _delete_all_tables(supabase_client)
