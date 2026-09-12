from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from services.notification_jobs import NotificationJobWorker, enqueue_flyer_published


def _dsn() -> str:
    return os.environ["DB_DSN"]


@pytest.fixture()
def notification_municipality_context():
    supermarket_id = str(uuid.uuid4())
    user_ids = {name: str(uuid.uuid4()) for name in (
        "same_municipality", "nearby_municipality", "far", "missing", "manager", "admin",
    )}
    conn = psycopg2.connect(_dsn())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            _insert_supermarket(cur, supermarket_id)
            _insert_users(cur, user_ids)
            _configure_profiles(cur, supermarket_id, user_ids)
        conn.commit()
        yield conn, supermarket_id, user_ids
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.notification_jobs WHERE payload->>'flyer_id' LIKE 'uat-%'")
            cur.execute("DELETE FROM auth.users WHERE id = ANY(%s::uuid[])", (list(user_ids.values()),))
            cur.execute("DELETE FROM public.supermarkets WHERE id = %s", (supermarket_id,))
        conn.commit()
        conn.close()


def test_flyer_notification_recipients_include_staff_and_nearby_customers(notification_municipality_context):
    conn, supermarket_id, user_ids = notification_municipality_context
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM public.flyer_notification_recipients(%s)", (supermarket_id,))
        recipients = {str(row[0]) for row in cur.fetchall()}
    assert recipients == {
        user_ids["same_municipality"], user_ids["nearby_municipality"], user_ids["manager"], user_ids["admin"],
    }


def test_flyer_notification_jobs_persist_inbox_without_push(
    notification_municipality_context,
    supabase_client,
):
    _, supermarket_id, user_ids = notification_municipality_context
    supabase_client.table("user_profiles").update({"notifications_enabled": False}).in_(
        "id", [user_ids["same_municipality"], user_ids["nearby_municipality"], user_ids["manager"], user_ids["admin"]]
    ).execute()
    enqueue_flyer_published(
        supabase_client,
        flyer_id=f"uat-{uuid.uuid4()}",
        supermarket_id=supermarket_id,
        supermarket_name="Supermercato UAT",
        products_count=4,
    )
    result = NotificationJobWorker(supabase_client).run_pending()
    inbox = supabase_client.table("app_notifications").select("user_id").execute().data
    assert result == {"claimed": 5, "processed": 5, "failed": 0}
    assert {row["user_id"] for row in inbox} == {
        user_ids["same_municipality"], user_ids["nearby_municipality"], user_ids["manager"], user_ids["admin"],
    }


def _insert_supermarket(cur, supermarket_id: str) -> None:
    cur.execute(
        """INSERT INTO public.supermarkets (id, name, slug, municipality_code)
           VALUES (%s, 'Supermercato UAT', %s, '015146')""",
        (supermarket_id, f"uat-notifications-{supermarket_id[:8]}"),
    )


def _insert_users(cur, user_ids: dict[str, str]) -> None:
    for name, user_id in user_ids.items():
        cur.execute(
            """INSERT INTO auth.users (id, email, encrypted_password, email_confirmed_at,
                  created_at, updated_at, raw_app_meta_data, raw_user_meta_data, aud, role)
               VALUES (%s, %s, '', NOW(), NOW(), NOW(), '{}'::jsonb, '{}'::jsonb,
                  'authenticated', 'authenticated')""",
            (user_id, f"uat-notifications-{name}-{user_id[:8]}@test.local"),
        )


def _configure_profiles(cur, supermarket_id: str, user_ids: dict[str, str]) -> None:
    cur.execute(
        """UPDATE public.user_profiles
           SET municipality_code = '015146', max_distance_km = 2
           WHERE id = %s""",
        (user_ids["same_municipality"],),
    )
    cur.execute(
        """UPDATE public.user_profiles
           SET municipality_code = '015205', max_distance_km = 10
           WHERE id = %s""",
        (user_ids["nearby_municipality"],),
    )
    cur.execute(
        """UPDATE public.user_profiles
           SET municipality_code = '058091', max_distance_km = 2
           WHERE id = %s""",
        (user_ids["far"],),
    )
    cur.execute(
        """UPDATE public.user_profiles SET role = 'supermarket_manager',
               managed_supermarket_id = %s
           WHERE id = %s""",
        (supermarket_id, user_ids["manager"]),
    )
    cur.execute(
        """UPDATE public.user_profiles SET role = 'admin'
           WHERE id = %s""",
        (user_ids["admin"],),
    )
