"""Unit tests for services/flyer_cleanup.py — no DB, no network."""

from __future__ import annotations

import sys
import os
import types
from datetime import date
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_settings = MagicMock()
_settings.supabase_url = "https://test.supabase.co"
_config_mod.settings = _settings
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

from services.flyer_cleanup import FlyerCleanupService  # noqa: E402

_TODAY = date(2026, 4, 27)
_FLYER_1 = {
    "id": "flyer-aaa",
    "supermarket_name": "Esselunga",
}
_FLYER_2 = {
    "id": "flyer-bbb",
    "supermarket_name": "Lidl",
}


def _make_sb(expired_flyers: list[dict], offer_counts: dict[str, int] | None = None) -> MagicMock:
    sb = MagicMock()
    offer_counts = offer_counts or {}
    flyers_table = MagicMock()
    offers_table = MagicMock()
    sb.table.side_effect = lambda name: offers_table if name == "offers" else flyers_table

    flyers_result = MagicMock()
    flyers_result.data = expired_flyers
    flyers_table.select.return_value.lt.return_value.not_.is_.return_value.execute.return_value = flyers_result

    def offers_select_side_effect(*args, **kwargs):
        if kwargs.get("count") != "exact":
            raise AssertionError("offers select should request exact count")
        select_result = MagicMock()

        def eq_side_effect(_column: str, flyer_id: str):
            exec_query = MagicMock()
            exec_query.execute.return_value = MagicMock(count=offer_counts.get(flyer_id, 0))
            return exec_query

        select_result.eq.side_effect = eq_side_effect
        return select_result

    offers_table.select.side_effect = offers_select_side_effect
    return sb


def _make_svc(sb: MagicMock, today: date = _TODAY) -> FlyerCleanupService:
    return FlyerCleanupService(supabase_factory=lambda: sb, today_factory=lambda: today)


class TestNoExpiredFlyers:
    def test_no_expired_returns_zero(self):
        sb = _make_sb([])
        assert _make_svc(sb).run() == 0
        sb.table.return_value.delete.assert_not_called()

    def test_query_filters_out_null_valid_to_with_is_null_syntax(self):
        sb = _make_sb([])

        _make_svc(sb).run()

        flyers_table = sb.table("flyers")
        flyers_table.select.return_value.lt.return_value.not_.is_.assert_called_once_with("valid_to", None)


class TestDeletesOffersOnly:
    def test_single_flyer_offers_deleted(self):
        sb = _make_sb([_FLYER_1], {"flyer-aaa": 2})
        result = _make_svc(sb).run()
        assert result == 2
        sb.storage.from_.assert_not_called()
        offers_table = sb.table("offers")
        offers_table.delete.return_value.eq.assert_called_with("flyer_id", "flyer-aaa")

    def test_multiple_flyers_all_deleted(self):
        sb = _make_sb([_FLYER_1, _FLYER_2], {"flyer-aaa": 2, "flyer-bbb": 3})
        result = _make_svc(sb).run()
        assert result == 5
        offers_table = sb.table("offers")
        assert offers_table.delete.return_value.eq.call_count == 2

    def test_zero_offer_flyer_is_skipped(self):
        sb = _make_sb([_FLYER_1], {"flyer-aaa": 0})
        _make_svc(sb).run()
        sb.table("offers").delete.assert_not_called()


class TestRowDeleteFailureContinues:
    def test_row_delete_failure_counted_and_next_flyer_attempted(self):
        sb = _make_sb([_FLYER_1, _FLYER_2], {"flyer-aaa": 2, "flyer-bbb": 3})
        sb.table("offers").delete.return_value.eq.return_value.execute.side_effect = [
            RuntimeError("db error"),
            MagicMock(),
        ]
        result = _make_svc(sb).run()
        assert result == 3

    def test_all_row_deletes_fail_returns_zero(self):
        sb = _make_sb([_FLYER_1], {"flyer-aaa": 2})
        sb.table("offers").delete.return_value.eq.return_value.execute.side_effect = RuntimeError("db error")
        result = _make_svc(sb).run()
        assert result == 0
