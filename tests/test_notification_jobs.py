from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from services.notification_jobs import (
    NotificationJobWorker,
    _flyer_notification_available_at,
    _supermarket_notification_location,
    enqueue_flyer_published,
    reschedule_flyer_published,
)


def test_enqueue_flyer_published_preserves_completed_jobs():
    sb = MagicMock()
    enqueue_flyer_published(
        sb, flyer_id="flyer-1", supermarket_id="sup-1", supermarket_name="Conad", products_count=12,
    )
    row = sb.table.return_value.upsert.call_args.args[0]
    assert row["idempotency_key"] == "flyer-published:flyer-1"
    sb.table.return_value.upsert.assert_called_once_with(
        row, on_conflict="idempotency_key", ignore_duplicates=True,
    )


def test_future_flyer_notification_waits_until_ten_in_rome():
    scheduled = _flyer_notification_available_at(
        "2026-08-19", now=datetime(2026, 8, 18, 12, tzinfo=UTC)
    )

    assert scheduled == datetime(2026, 8, 19, 8, tzinfo=UTC)


def test_past_flyer_notification_is_available_immediately():
    now = datetime(2026, 8, 19, 9, tzinfo=UTC)

    assert _flyer_notification_available_at("2026-08-19", now=now) == now


def test_supermarket_notification_location_uses_municipality():
    sb = MagicMock()
    result = sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value
    result.data = {"municipalities": {"name": "Milano", "province_code": "MI"}}

    assert _supermarket_notification_location(sb, "super-1") == "Milano (MI)"


def test_supermarket_notification_location_falls_back_to_municipality_name():
    sb = MagicMock()
    result = sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value
    result.data = {"municipalities": {"name": "Milano", "province_code": None}}

    assert _supermarket_notification_location(sb, "super-1") == "Milano"


def test_reschedule_updates_only_undelivered_parent_job():
    sb = MagicMock()

    with patch(
        "services.notification_jobs._flyer_notification_available_at",
        return_value=datetime(2026, 8, 19, 8, tzinfo=UTC),
    ):
        reschedule_flyer_published(
            sb, flyer_id="flyer-1", valid_from="2026-08-19"
        )

    update = sb.table.return_value.update.call_args.args[0]
    assert update["available_at"] == "2026-08-19T08:00:00+00:00"
    statuses = (
        sb.table.return_value.update.return_value.eq.return_value.eq.return_value.in_
        .call_args.args
    )
    assert statuses == ("status", ["pending", "failed"])


def test_parent_materializes_one_idempotent_job_per_recipient():
    sb = MagicMock()
    payload = _payload()
    sb.rpc.return_value.execute.return_value.data = [{"user_id": "user-1"}, {"user_id": "user-2"}]

    with (
        patch(
            "services.notification_jobs._flyer_is_notification_eligible",
            return_value=True,
        ),
        patch(
            "services.notification_jobs._supermarket_notification_location",
            return_value="Via Roma 1, Milano",
        ),
    ):
        NotificationJobWorker(sb)._dispatch({"kind": "flyer_published", "payload": payload}, sb)

    assert sb.rpc.call_args.args[0] == "flyer_notification_recipients"
    keys = [call.args[0]["idempotency_key"] for call in sb.table.return_value.upsert.call_args_list]
    assert keys == ["flyer-published:flyer-1:user-1", "flyer-published:flyer-1:user-2"]
    first_payload = sb.table.return_value.upsert.call_args_list[0].args[0]["payload"]
    assert first_payload["supermarket_location"] == "Via Roma 1, Milano"


def test_recipient_jobs_use_independent_clients_and_run_in_parallel():
    sb = MagicMock()
    clients = [MagicMock(), MagicMock()]
    issued_clients: list[MagicMock] = []

    def client_factory() -> MagicMock:
        client = clients.pop()
        issued_clients.append(client)
        return client

    worker = NotificationJobWorker(sb, client_factory=client_factory)
    jobs = [_job("job-1", "user-1"), _job("job-2", "user-2")]

    with (
        patch("services.notification_jobs.settings.notification_delivery_workers", 2),
        patch("services.notification_jobs._flyer_is_notification_eligible", return_value=True),
        patch("services.notification_jobs.deliver_public_flyer_published_to_recipient") as deliver,
    ):
        results = worker._run_recipients(jobs)

    assert results == [True, True]
    assert deliver.call_count == 2
    assert {call.args[0] for call in deliver.call_args_list} == set(issued_clients)


def test_failed_recipient_is_retried_without_reprocessing_parent():
    sb = MagicMock()
    job = _job("recipient-job", "user-1", attempts=1)
    recipient_sb = MagicMock()
    worker = NotificationJobWorker(sb, client_factory=lambda: recipient_sb)

    with patch(
        "services.notification_jobs.deliver_public_flyer_published_to_recipient",
        side_effect=RuntimeError("push unavailable"),
    ), patch(
        "services.notification_jobs._flyer_is_notification_eligible", return_value=True
    ):
        assert worker._run_recipient_job(job) is False

    update = recipient_sb.table.return_value.update.call_args.args[0]
    assert update["status"] == "failed"


def test_ineligible_flyer_never_materializes_recipients():
    sb = MagicMock()

    with patch(
        "services.notification_jobs._flyer_is_notification_eligible", return_value=False
    ):
        NotificationJobWorker(sb)._dispatch({"kind": "flyer_published", "payload": _payload()}, sb)

    sb.rpc.assert_not_called()


def _payload() -> dict:
    return {
        "flyer_id": "flyer-1", "supermarket_id": "sup-1",
        "supermarket_name": "Conad", "products_count": 3,
    }


def _job(job_id: str, user_id: str, attempts: int = 1) -> dict:
    return {
        "id": job_id, "kind": "flyer_published_recipient",
        "payload": {**_payload(), "user_id": user_id},
        "attempts": attempts, "max_attempts": 5,
    }
