"""Unit tests for services/repositories/notifications_repository.py."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

from services.repositories import notifications_repository


class _Result:
    def __init__(self, data):
        self.data = data


class _SelectTable:
    def __init__(self, rows: list[dict], *, snapshots: list[list[dict]] | None = None):
        self.rows = rows
        self.filters: dict[str, object] = {}
        self.snapshots = snapshots or []

    def select(self, _fields: str):
        return self

    def eq(self, key: str, value: object):
        self.filters[key] = value
        return self

    def in_(self, key: str, values: list[str]):
        self.filters[key] = list(values)
        return self

    def execute(self):
        if self.snapshots:
            snapshot_rows = self.snapshots.pop(0)
            return _Result([{"id": row["id"]} for row in snapshot_rows])
        matched_rows: list[dict] = []
        for row in self.rows:
            matches = True
            for key, value in self.filters.items():
                if isinstance(value, list):
                    matches = row.get(key) in value
                else:
                    matches = row.get(key) == value
                if not matches:
                    break
            if matches:
                matched_rows.append({"id": row["id"]})
        return _Result(matched_rows)


class _DeleteTable:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.filters: dict[str, object] = {}
        self.result_data: list[dict] | None = None

    def delete(self):
        return self

    def eq(self, key: str, value: object):
        self.filters[key] = value
        return self

    def in_(self, key: str, values: list[str]):
        self.filters[key] = list(values)
        return self

    def execute(self):
        result_rows = list(self.result_data) if self.result_data is not None else []
        if self.result_data is None:
            for row in self.rows:
                matches = True
                for key, value in self.filters.items():
                    if isinstance(value, list):
                        matches = row.get(key) in value
                    else:
                        matches = row.get(key) == value
                    if not matches:
                        break
                if matches:
                    result_rows.append({"id": row["id"]})
        deleted_rows: list[dict] = []
        remaining_rows: list[dict] = []
        for row in self.rows:
            matches = True
            for key, value in self.filters.items():
                if isinstance(value, list):
                    matches = row.get(key) in value
                else:
                    matches = row.get(key) == value
                if not matches:
                    break
            if matches:
                deleted_rows.append(row)
            else:
                remaining_rows.append(row)
        self.rows[:] = remaining_rows
        return _Result(result_rows)


class _NotificationsSupabase:
    def __init__(
        self,
        rows: list[dict],
        *,
        delete_result_data: list[dict] | None = None,
        select_snapshots: list[list[dict]] | None = None,
    ):
        self.rows = rows
        self.delete_result_data = delete_result_data
        self.select_snapshots = select_snapshots or []
        self.table_calls: list[str] = []

    def table(self, name: str):
        self.table_calls.append(name)
        delete_table = _DeleteTable(self.rows)
        delete_table.result_data = self.delete_result_data
        select_table = _SelectTable(self.rows, snapshots=self.select_snapshots)
        return _NotificationsTable(select_table, delete_table)


class _NotificationsTable:
    def __init__(self, select_table: _SelectTable, delete_table: _DeleteTable):
        self._select_table = select_table
        self._delete_table = delete_table

    def select(self, fields: str):
        return self._select_table.select(fields)

    def delete(self):
        return self._delete_table.delete()


class _UnusedSupabase:
    def table(self, _name: str):
        raise AssertionError("Supabase should not be touched for empty notification_ids")


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.rowcount = 0
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._fetchall_rows: list[dict] = []

    def execute(self, query: str, params: tuple[object, ...]):
        self.executed.append((query, params))
        compact_query = " ".join(query.split())
        if "RETURNING id" in compact_query:
            user_id, notification_ids = params
            deleted = [
                {"id": row["id"]}
                for row in self.rows
                if row["user_id"] == user_id and row["id"] in notification_ids
            ]
            self.rows[:] = [
                row
                for row in self.rows
                if not (row["user_id"] == user_id and row["id"] in notification_ids)
            ]
            self._fetchall_rows = deleted
            self.rowcount = len(deleted)
            return
        notification_id, user_id = params
        deleted = [
            row
            for row in self.rows
            if row["id"] == notification_id and row["user_id"] == user_id
        ]
        self.rows[:] = [
            row
            for row in self.rows
            if not (row["id"] == notification_id and row["user_id"] == user_id)
        ]
        self.rowcount = len(deleted)

    def fetchall(self):
        return list(self._fetchall_rows)


class _CursorCtx:
    def __init__(self, cursor: _Cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        return False


def test_delete_notification_scopes_by_user_in_supabase_mode(monkeypatch):
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-2"},
    ]
    fake = _NotificationsSupabase(
        rows,
        delete_result_data=[],
        select_snapshots=[
            [{"id": "11111111-1111-1111-1111-111111111111"}],
            [],
        ],
    )
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: False)
    monkeypatch.setattr(notifications_repository, "get_supabase", lambda: fake)

    deleted = notifications_repository.delete_notification(
        "11111111-1111-1111-1111-111111111111",
        "user-1",
    )

    assert deleted is True
    assert rows == [{"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-2"}]
    assert fake.table_calls == [
        "app_notifications",
        "app_notifications",
        "app_notifications",
    ]


def test_delete_notification_returns_false_when_post_delete_check_still_finds_row(monkeypatch):
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "22222222-2222-2222-2222-222222222222", "user_id": "user-2"},
    ]
    fake = _NotificationsSupabase(
        rows,
        delete_result_data=[],
        select_snapshots=[
            [{"id": "11111111-1111-1111-1111-111111111111"}],
            [{"id": "11111111-1111-1111-1111-111111111111"}],
        ],
    )
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: False)
    monkeypatch.setattr(notifications_repository, "get_supabase", lambda: fake)

    deleted = notifications_repository.delete_notification(
        "11111111-1111-1111-1111-111111111111",
        "user-1",
    )

    assert deleted is False


def test_delete_notifications_returns_deleted_and_missing_ids_in_supabase_mode(monkeypatch):
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "22222222-2222-2222-2222-222222222222", "user_id": "user-2"},
    ]
    fake = _NotificationsSupabase(
        rows,
        delete_result_data=[],
        select_snapshots=[
            [{"id": "11111111-1111-1111-1111-111111111111"}],
            [],
        ],
    )
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: False)
    monkeypatch.setattr(notifications_repository, "get_supabase", lambda: fake)

    result = notifications_repository.delete_notifications(
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ],
        "user-1",
    )

    assert result == {
        "deleted_ids": ["11111111-1111-1111-1111-111111111111"],
        "missing_ids": [
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ],
    }
    assert rows == [{"id": "22222222-2222-2222-2222-222222222222", "user_id": "user-2"}]
    assert fake.table_calls == [
        "app_notifications",
        "app_notifications",
        "app_notifications",
    ]


def test_delete_notifications_marks_preselected_ids_missing_when_post_delete_check_still_finds_them(
    monkeypatch,
):
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "22222222-2222-2222-2222-222222222222", "user_id": "user-1"},
    ]
    fake = _NotificationsSupabase(
        rows,
        delete_result_data=[],
        select_snapshots=[
            [
                {"id": "11111111-1111-1111-1111-111111111111"},
                {"id": "22222222-2222-2222-2222-222222222222"},
            ],
            [{"id": "22222222-2222-2222-2222-222222222222"}],
        ],
    )
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: False)
    monkeypatch.setattr(notifications_repository, "get_supabase", lambda: fake)

    result = notifications_repository.delete_notifications(
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ],
        "user-1",
    )

    assert result == {
        "deleted_ids": ["11111111-1111-1111-1111-111111111111"],
        "missing_ids": ["22222222-2222-2222-2222-222222222222"],
    }


def test_delete_notifications_returns_empty_result_without_touching_storage(monkeypatch):
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: False)
    monkeypatch.setattr(notifications_repository, "get_supabase", lambda: _UnusedSupabase())

    result = notifications_repository.delete_notifications([], "user-1")

    assert result == {"deleted_ids": [], "missing_ids": []}


def test_delete_notification_scopes_by_user_in_direct_postgres_mode(monkeypatch):
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-2"},
    ]
    cursor = _Cursor(rows)
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: True)
    monkeypatch.setattr(
        notifications_repository,
        "get_postgres_cursor",
        lambda: _CursorCtx(cursor),
    )

    deleted = notifications_repository.delete_notification(
        "11111111-1111-1111-1111-111111111111",
        "user-1",
    )

    assert deleted is True
    assert rows == [{"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-2"}]
    assert cursor.executed[0][1] == (
        "11111111-1111-1111-1111-111111111111",
        "user-1",
    )


def test_delete_notifications_returns_deleted_and_missing_ids_in_direct_postgres_mode(monkeypatch):
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "22222222-2222-2222-2222-222222222222", "user_id": "user-1"},
        {"id": "33333333-3333-3333-3333-333333333333", "user_id": "user-2"},
    ]
    cursor = _Cursor(rows)
    monkeypatch.setattr(notifications_repository, "has_direct_postgres", lambda: True)
    monkeypatch.setattr(
        notifications_repository,
        "get_postgres_cursor",
        lambda: _CursorCtx(cursor),
    )

    result = notifications_repository.delete_notifications(
        [
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
        "user-1",
    )

    assert result == {
        "deleted_ids": ["22222222-2222-2222-2222-222222222222"],
        "missing_ids": [
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    }
    assert rows == [
        {"id": "11111111-1111-1111-1111-111111111111", "user_id": "user-1"},
        {"id": "33333333-3333-3333-3333-333333333333", "user_id": "user-2"},
    ]
    assert cursor.executed[0][1] == (
        "user-1",
        [
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    )
    assert "id = ANY(%s::uuid[])" in cursor.executed[0][0]
