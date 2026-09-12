"""Unit tests for services/purchased_items_cleanup.py — no DB, no network."""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

sys.modules["core.database"] = MagicMock()

from services.purchased_items_cleanup import PurchasedItemsCleanupService  # noqa: E402

_TODAY = date(2026, 5, 7)


def _make_sb(lists: list[dict]) -> MagicMock:
    sb = MagicMock()
    result = MagicMock()
    result.data = lists
    sb.table.return_value.select.return_value.execute.return_value = result
    return sb


def _make_svc(sb: MagicMock, today: date = _TODAY) -> PurchasedItemsCleanupService:
    return PurchasedItemsCleanupService(
        supabase_factory=lambda: sb,
        today_factory=lambda: today,
    )


def _item(
    item_id: str,
    *,
    purchased: bool,
    purchased_at: str | None,
) -> dict:
    return {
        "id": item_id,
        "name": item_id,
        "purchased": purchased,
        "purchased_at": purchased_at,
    }


class TestPurchasedItemsCleanup:
    def test_returns_zero_when_no_lists_exist(self):
        sb = _make_sb([])
        assert _make_svc(sb).run() == 0
        sb.table.return_value.update.assert_not_called()

    def test_keeps_unpurchased_and_today_purchased_items(self):
        shopping_list = {
            "id": "list-1",
            "items": [
                _item("active", purchased=False, purchased_at=None),
                _item("today", purchased=True, purchased_at="2026-05-07T08:30:00+00:00"),
            ],
        }
        sb = _make_sb([shopping_list])

        result = _make_svc(sb).run()

        assert result == 0
        sb.table.return_value.update.assert_not_called()

    def test_removes_items_purchased_before_today(self):
        shopping_list = {
            "id": "list-1",
            "items": [
                _item("old", purchased=True, purchased_at="2026-05-06T21:30:00+00:00"),
                _item("today", purchased=True, purchased_at="2026-05-07T08:30:00+00:00"),
                _item("active", purchased=False, purchased_at=None),
            ],
        }
        sb = _make_sb([shopping_list])

        result = _make_svc(sb).run()

        assert result == 1
        updated_items = sb.table.return_value.update.call_args.args[0]["items"]
        assert [item["id"] for item in updated_items] == ["today", "active"]

    def test_rome_timezone_boundary_removes_pre_midnight_item(self):
        shopping_list = {
            "id": "list-1",
            "items": [
                _item("before-midnight", purchased=True, purchased_at="2026-05-06T21:59:59+00:00"),
                _item("after-midnight", purchased=True, purchased_at="2026-05-06T22:00:00+00:00"),
            ],
        }
        sb = _make_sb([shopping_list])

        result = _make_svc(sb).run()

        assert result == 1
        updated_items = sb.table.return_value.update.call_args.args[0]["items"]
        assert [item["id"] for item in updated_items] == ["after-midnight"]

    def test_updates_multiple_lists_and_returns_removed_count(self):
        shopping_lists = [
            {
                "id": "list-1",
                "items": [_item("old-1", purchased=True, purchased_at="2026-05-05T10:00:00+00:00")],
            },
            {
                "id": "list-2",
                "items": [
                    _item("old-2", purchased=True, purchased_at="2026-05-06T10:00:00+00:00"),
                    _item("active", purchased=False, purchased_at=None),
                ],
            },
        ]
        sb = _make_sb(shopping_lists)

        result = _make_svc(sb).run()

        assert result == 2
        assert sb.table.return_value.update.return_value.eq.return_value.execute.call_count == 2
