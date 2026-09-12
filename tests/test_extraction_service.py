"""Unit tests for services/extraction/service.py — ExtractionService."""

from __future__ import annotations

import sys
import os
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# Stub infrastructure modules
# ---------------------------------------------------------------------------
for _mod in ("supabase", "jose", "jose.jwt", "requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_settings = MagicMock()
_settings.llm_provider = "gemini"
_settings.google_api_key = "test-key"
_settings.gemini_model = "gemma-4-31b-it"
_config_mod.settings = _settings
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

import pytest


def test_resume_keeps_checkpointed_pdf_chunk_size() -> None:
    from services.extraction.service import ExtractionService

    provider = MagicMock()
    provider.chunk_size_pages = 2
    state = ExtractionService(provider=provider)._resume_state(
        {
            "extraction_metadata": {
                "chunk_size_pages": 3,
                "last_completed_chunk": 1,
                "next_chunk_index": 2,
                "resume_available": True,
            }
        },
        "application/pdf",
        24,
    )

    assert state["chunk_size_pages"] == 3
    assert state["start_chunk_index"] == 2


def _make_sb(
    flyer_data: dict | None = None,
    upsert_data: list | None = None,
    select_fallback_data: list | None = None,
) -> MagicMock:
    """Build a Supabase mock that covers the full extraction pipeline."""
    sb = MagicMock()

    # _fetch_flyer — .select().eq().single().execute()
    flyer_result = MagicMock()
    flyer_result.data = flyer_data or {
        "id": "flyer-1",
        "file_url": "https://example.com/flyer.jpg",
        "file_name": "flyer.jpg",
        "supermarket_id": "sup-1",
        "supermarket_name": "Test Super",
        "valid_from": None,
        "valid_to": "2026-05-01",
        "user_id": "user-1",
    }

    # _upsert_product — .upsert().execute()
    upsert_result = MagicMock()
    upsert_result.data = upsert_data if upsert_data is not None else [{"id": "prod-uuid"}]

    # extraction_log insert
    insert_result = MagicMock()
    insert_result.data = [{"id": "log-uuid"}]

    # flyers.update().eq().execute()
    update_result = MagicMock()
    update_result.data = []

    # Build chained mock
    table_mock = MagicMock()
    sb.table.return_value = table_mock

    # .select(...).eq(...).single().execute() → flyer
    table_mock.select.return_value.eq.return_value.single.return_value.execute.return_value = flyer_result
    table_mock.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = flyer_result
    # .upsert(...).execute() → products and offers
    table_mock.upsert.return_value.execute.return_value = upsert_result
    # .insert(...).execute() → extraction log
    table_mock.insert.return_value.execute.return_value = insert_result
    # .update(...).eq(...).execute() → flyer status update
    table_mock.update.return_value.eq.return_value.execute.return_value = update_result

    def _update_side_effect(payload: dict):
        if isinstance(flyer_result.data, dict):
            flyer_result.data.update(payload)
        return table_mock.update.return_value

    table_mock.update.side_effect = _update_side_effect

    return sb


def _offer_upsert_calls(sb: MagicMock) -> list:
    return [
        call
        for call in sb.table.return_value.upsert.call_args_list
        if call.kwargs.get("on_conflict") == "flyer_id,offer_key,format_key"
    ]


def _product_upsert_calls(sb: MagicMock) -> list:
    return [
        call
        for call in sb.table.return_value.upsert.call_args_list
        if call.kwargs.get("on_conflict") == "name,brand"
    ]


def _extraction_log_insert_payloads(sb: MagicMock) -> list[dict]:
    return [
        call[0][0]
        for call in sb.table.return_value.insert.call_args_list
        if call[0] and isinstance(call[0][0], dict) and "event_type" in call[0][0]
    ]


_EXTRACTED_PRODUCTS = [
    {
        "name": "Pasta Barilla",
        "brand": "Barilla",
        "category": "dispensa",
        "format": {
            "tipo": "confezione_singola",
            "peso_volume": 500,
            "unita_misura": "g",
        },
        "price_offer": 1.29,
        "price_original": 1.79,
        "valid_from": "2026-04-21",
        "valid_to": "2026-04-27",
    }
]

_EXTRACTED_PRODUCTS_V2 = [
    {
        "name": "Tonno all'olio di oliva",
        "brand": "Rio Mare",
        "category_main": "Dispensa",
        "category_sub": "Conserve Ittiche e di Carne",
        "format": {
            "tipo": "multipack_omogeneo",
            "quantita": 2,
            "peso_volume": 80,
            "unita_misura": "g",
        },
        "price_current": 4.99,
        "price_original": 6.79,
        "discount_percentage": 26,
        "price_per_unit": 31.19,
        "price_per_unit_measure": "kg",
        "valid_from": "2026-04-21",
        "valid_to": "2026-04-27",
    }
]


class TestExtractionServiceRunSetsOfferAsUnconfirmed:
    """is_confirmed must be False on all inserted offers."""

    def test_run_inserts_draft_offers_not_confirmed(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS, [])

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        offer_upserts = _offer_upsert_calls(sb)
        assert len(offer_upserts) >= 1, "Expected at least one batch offer upsert"
        offer_rows = offer_upserts[0][0][0]
        assert isinstance(offer_rows, list)
        for row in offer_rows:
            assert row["is_confirmed"] is False


class TestExtractionServiceStatusTransitions:
    """Flyer status set to 'done' on success."""

    def test_run_sets_flyer_status_done(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS, [])

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        update_calls = sb.table.return_value.update.call_args_list
        done_calls = [c for c in update_calls if c[0][0].get("status") == "done"]
        assert len(done_calls) >= 1

    def test_run_persists_structured_unit_price_fields(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS_V2, [])

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        offer_rows = _offer_upsert_calls(sb)[0][0][0]
        assert offer_rows[0]["unit_price_value"] == pytest.approx(31.19)
        assert offer_rows[0]["unit_price_unit"] == "kg"
        assert offer_rows[0]["unit_price"] == "31,19 €/kg"


class TestExtractionServiceErrorPath:
    """On provider failure, flyer status must be set to 'error'."""

    def test_run_provider_failure_sets_error(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.side_effect = RuntimeError("Gemini timeout")

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")  # must not raise

        update_calls = sb.table.return_value.update.call_args_list
        error_calls = [c for c in update_calls if c[0][0].get("status") == "error"]
        assert len(error_calls) >= 1

    def test_run_late_failure_after_completed_pdf_keeps_flyer_done(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
                "status": "pending",
                "extraction_metadata": None,
            }
        )
        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS, [])

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=3),
            patch(
                "services.extraction.service.notify_extraction_complete",
                side_effect=[RuntimeError("[Errno 11] Resource temporarily unavailable"), None],
            ),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        update_calls = sb.table.return_value.update.call_args_list
        error_calls = [c for c in update_calls if c[0][0].get("status") == "error"]
        done_payloads = [c[0][0] for c in update_calls if c[0][0].get("status") == "done"]

        assert not error_calls
        assert done_payloads
        assert done_payloads[-1]["products_count"] == 1
        assert done_payloads[-1]["extraction_metadata"]["resume_available"] is False

        error_log_payload = _extraction_log_insert_payloads(sb)[-1]
        assert error_log_payload["event_type"] == "error"
        assert (
            error_log_payload["details"]["resume"]["reason"]
            == "completed_persistence_after_late_runtime_failure"
        )

    def test_run_keeps_partial_chunk_offers_and_marks_resume_metadata(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
                "status": "pending",
                "extraction_metadata": None,
            }
        )
        latest_metadata: dict | None = None

        def _capture_update(payload):
            nonlocal latest_metadata
            if isinstance(payload, dict) and isinstance(payload.get("extraction_metadata"), dict):
                latest_metadata = payload["extraction_metadata"]
            return sb.table.return_value.update.return_value

        sb.table.return_value.update.side_effect = _capture_update
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.side_effect = (
            lambda: MagicMock(data={"extraction_metadata": latest_metadata})
        )

        def _extract_products(
            file_bytes,
            mime_type,
            progress_callback=None,
            chunk_result_callback=None,
            start_chunk_index=1,
        ):
            from services.extraction.providers.base import PdfChunkExtractionError

            assert start_chunk_index == 1
            if chunk_result_callback:
                chunk_result_callback(
                    {
                        "chunk_index": 1,
                        "chunks_total": 2,
                        "current_chunk_start": 1,
                        "current_chunk_end": 3,
                        "products": _EXTRACTED_PRODUCTS,
                        "retry_errors": [],
                    }
                )
            if progress_callback:
                progress_callback(
                    {
                        "chunks_completed": 1,
                        "chunks_total": 2,
                        "current_chunk_start": 1,
                        "current_chunk_end": 3,
                        "pages_processed": 3,
                        "products_found": 1,
                    }
                )
                progress_callback(
                    {
                        "chunks_completed": 1,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "pages_processed": 3,
                        "products_found": 1,
                    }
                )
            raise PdfChunkExtractionError(
                chunk_index=2,
                chunks_total=2,
                start_page=4,
                end_page=6,
                retry_errors=["Chunk 2 failed"],
            )

        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = _extract_products

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=6),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService

            ExtractionService(provider=mock_provider, supabase_factory=lambda: sb).run("flyer-1")

        assert len(_offer_upsert_calls(sb)) == 1
        error_payloads = [
            c[0][0] for c in sb.table.return_value.update.call_args_list if c[0][0].get("status") == "error"
        ]
        assert error_payloads
        metadata = error_payloads[-1]["extraction_metadata"]
        assert metadata["resume_available"] is True
        assert metadata["failed_chunk_index"] == 2
        assert metadata["failed_chunk_start"] == 4
        assert metadata["failed_chunk_end"] == 6
        assert metadata["next_chunk_index"] == 2
        assert metadata["partial_products_count"] == 1

    def test_run_resumes_from_saved_pdf_chunk_state_without_duplicate_offers(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
                "status": "error",
                "extraction_metadata": {
                    "stage": "extracting",
                    "extraction_started_at": "2026-05-08T10:00:00Z",
                    "resume_available": True,
                    "next_chunk_index": 2,
                    "next_chunk_start": 4,
                    "next_chunk_end": 6,
                    "partial_products_count": 1,
                    "products_raw_count": 1,
                    "products_after_variants_count": 1,
                    "products_unique_count": 1,
                    "last_completed_chunk": 1,
                    "chunks_completed": 1,
                    "chunks_total": 2,
                    "chunk_failures": 1,
                },
            }
        )

        def _extract_products(
            file_bytes,
            mime_type,
            progress_callback=None,
            chunk_result_callback=None,
            start_chunk_index=1,
        ):
            assert start_chunk_index == 2
            if chunk_result_callback:
                chunk_result_callback(
                    {
                        "chunk_index": 2,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "products": _EXTRACTED_PRODUCTS,
                        "retry_errors": [],
                    }
                )
            if progress_callback:
                progress_callback(
                    {
                        "chunks_completed": 2,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "pages_processed": 6,
                        "products_found": 2,
                    }
                )
            return (_EXTRACTED_PRODUCTS, [])

        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = _extract_products

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=6),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService

            ExtractionService(provider=mock_provider, supabase_factory=lambda: sb).run("flyer-1")

        assert len(_offer_upsert_calls(sb)) == 1
        done_payloads = [
            c[0][0] for c in sb.table.return_value.update.call_args_list if c[0][0].get("status") == "done"
        ]
        assert done_payloads
        assert done_payloads[-1]["products_count"] == 2

    def test_run_resumes_even_after_router_flips_status_to_processing(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
                "status": "processing",
                "extraction_metadata": {
                    "stage": "extracting",
                    "extraction_started_at": "2026-05-08T10:00:00Z",
                    "resume_available": True,
                    "next_chunk_index": 2,
                    "next_chunk_start": 4,
                    "next_chunk_end": 6,
                    "partial_products_count": 1,
                    "products_raw_count": 1,
                    "products_after_variants_count": 1,
                    "products_unique_count": 1,
                    "last_completed_chunk": 1,
                    "chunks_completed": 1,
                    "chunks_total": 2,
                    "chunk_failures": 1,
                },
            }
        )

        def _extract_products(
            file_bytes,
            mime_type,
            progress_callback=None,
            chunk_result_callback=None,
            start_chunk_index=1,
        ):
            assert start_chunk_index == 2
            if chunk_result_callback:
                chunk_result_callback(
                    {
                        "chunk_index": 2,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "products": _EXTRACTED_PRODUCTS,
                        "retry_errors": [],
                    }
                )
            if progress_callback:
                progress_callback(
                    {
                        "chunks_completed": 2,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "pages_processed": 6,
                        "products_found": 2,
                    }
                )
            return (_EXTRACTED_PRODUCTS, [])

        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = _extract_products

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=6),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService

            ExtractionService(provider=mock_provider, supabase_factory=lambda: sb).run("flyer-1")

        assert len(_offer_upsert_calls(sb)) == 1
        done_payloads = [
            c[0][0] for c in sb.table.return_value.update.call_args_list if c[0][0].get("status") == "done"
        ]
        assert done_payloads
        assert done_payloads[-1]["products_count"] == 2

    def test_run_marks_generic_failure_after_saved_chunk_as_resumable(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )

        def _extract_products(
            file_bytes,
            mime_type,
            progress_callback=None,
            chunk_result_callback=None,
            start_chunk_index=1,
        ):
            assert start_chunk_index == 1
            if chunk_result_callback:
                chunk_result_callback(
                    {
                        "chunk_index": 1,
                        "chunks_total": 2,
                        "current_chunk_start": 1,
                        "current_chunk_end": 3,
                        "products": _EXTRACTED_PRODUCTS,
                        "retry_errors": [],
                    }
                )
            if progress_callback:
                progress_callback(
                    {
                        "chunks_completed": 1,
                        "chunks_total": 2,
                        "current_chunk_start": 1,
                        "current_chunk_end": 3,
                        "pages_processed": 3,
                        "products_found": 1,
                    }
                )
            raise RuntimeError("transient supabase read failed")

        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = _extract_products

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=6),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService

            ExtractionService(provider=mock_provider, supabase_factory=lambda: sb).run("flyer-1")

        error_payloads = [
            c[0][0] for c in sb.table.return_value.update.call_args_list if c[0][0].get("status") == "error"
        ]
        assert error_payloads
        metadata = error_payloads[-1]["extraction_metadata"]
        assert metadata["resume_available"] is True
        assert metadata["next_chunk_index"] == 2
        assert metadata["next_chunk_start"] == 4
        assert metadata["next_chunk_end"] == 6
        assert metadata["partial_products_count"] == 1
        assert "failed_chunk_index" not in metadata

        error_log_payload = _extraction_log_insert_payloads(sb)[-1]
        assert error_log_payload["details"]["resume"]["available"] is True
        assert error_log_payload["details"]["resume"]["reason"] == "generic_runtime_failure_after_persisted_chunk"

    def test_run_resumes_from_generic_failure_metadata_without_resume_flag(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
                "status": "error",
                "extraction_metadata": {
                    "stage": "extracting",
                    "extraction_started_at": "2026-05-08T10:00:00Z",
                    "resume_available": False,
                    "next_chunk_index": 2,
                    "next_chunk_start": 4,
                    "next_chunk_end": 6,
                    "partial_products_count": 1,
                    "products_raw_count": 1,
                    "products_after_variants_count": 1,
                    "products_unique_count": 1,
                    "last_completed_chunk": 1,
                    "chunks_completed": 1,
                    "chunks_total": 2,
                    "chunk_failures": 0,
                },
            }
        )

        def _extract_products(
            file_bytes,
            mime_type,
            progress_callback=None,
            chunk_result_callback=None,
            start_chunk_index=1,
        ):
            assert start_chunk_index == 2
            if chunk_result_callback:
                chunk_result_callback(
                    {
                        "chunk_index": 2,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "products": _EXTRACTED_PRODUCTS,
                        "retry_errors": [],
                    }
                )
            if progress_callback:
                progress_callback(
                    {
                        "chunks_completed": 2,
                        "chunks_total": 2,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "pages_processed": 6,
                        "products_found": 2,
                    }
                )
            return (_EXTRACTED_PRODUCTS, [])

        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = _extract_products

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=6),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService

            ExtractionService(provider=mock_provider, supabase_factory=lambda: sb).run("flyer-1")

        assert len(_offer_upsert_calls(sb)) == 1
        done_payloads = [
            c[0][0] for c in sb.table.return_value.update.call_args_list if c[0][0].get("status") == "done"
        ]
        assert done_payloads
        assert done_payloads[-1]["products_count"] == 2

    def test_run_keeps_generic_failure_without_saved_chunks_non_resumable(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )
        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = RuntimeError("boom before first chunk")

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=6),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService

            ExtractionService(provider=mock_provider, supabase_factory=lambda: sb).run("flyer-1")

        error_payloads = [
            c[0][0] for c in sb.table.return_value.update.call_args_list if c[0][0].get("status") == "error"
        ]
        assert error_payloads
        metadata = error_payloads[-1]["extraction_metadata"]
        assert metadata["resume_available"] is False
        assert metadata["next_chunk_index"] == 1

        error_log_payload = _extraction_log_insert_payloads(sb)[-1]
        assert error_log_payload["details"]["resume"]["available"] is False
        assert error_log_payload["details"]["resume"]["reason"] == "not_available"


class TestExtractionServiceSubcategoryPersisted:
    """Subcategory from LLM response must reach draft offers."""

    def test_subcategory_written_to_upsert(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS_V2, [])

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        assert _product_upsert_calls(sb) == []

        offer_calls = _offer_upsert_calls(sb)
        assert offer_calls, "Expected at least one offer upsert"
        offer_row = offer_calls[0][0][0][0]
        assert offer_row.get("subcategory") == "Conserve Ittiche e di Carne"
        assert offer_row.get("format_key")
        assert offer_row.get("format_label") == "2x80 g"

    def test_duplicate_offer_conflict_keys_are_sent_once(self):
        with patch("services.extraction.service.get_provider", return_value=MagicMock()):
            from services.extraction.service import ExtractionService

            svc = ExtractionService()

        rows = [
            {"offer_key": "pasta|barilla", "flyer_id": "flyer-1", "format_key": "v1:500g", "price_offer": 1.29},
            {"offer_key": "pasta|barilla", "flyer_id": "flyer-1", "format_key": "v1:500g", "price_offer": 1.49},
            {"offer_key": "pasta|barilla", "flyer_id": "flyer-1", "format_key": "v1:1kg", "price_offer": 2.49},
        ]

        unique_rows = svc._deduplicate_offer_rows(rows)

        assert unique_rows == [rows[0], rows[2]]

    def test_batch_offer_upsert_is_used_for_multiple_draft_products(self):
        sb = _make_sb(
            upsert_data=[
                {"id": "prod-1"},
                {"id": "prod-2"},
            ]
        )
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (
            [
                {
                    "name": "Pasta Barilla",
                    "brand": "Barilla",
                    "category": "dispensa",
                    "format": {
                        "tipo": "confezione_singola",
                        "peso_volume": 500,
                        "unita_misura": "g",
                    },
                    "price_offer": 1.29,
                },
                {
                    "name": "Latte Berna",
                    "brand": "Berna",
                    "category": "alimentari-freschi",
                    "format": {
                        "tipo": "confezione_singola",
                        "peso_volume": 1,
                        "unita_misura": "L",
                    },
                    "price_offer": 1.59,
                },
            ],
            [],
        )

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        assert _product_upsert_calls(sb) == []
        offer_calls = _offer_upsert_calls(sb)
        assert len(offer_calls) == 1
        batch_payload = offer_calls[0][0][0]
        assert isinstance(batch_payload, list)
        assert len(batch_payload) == 2
        assert {row["offer_key"] for row in batch_payload} == {
            "pasta barilla|barilla",
            "latte berna|berna",
        }

    def test_done_metadata_includes_stage_timings_and_format_sizes(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS_V2, [])
        mock_provider.chunk_size_pages = 3

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=7),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        update_calls = sb.table.return_value.update.call_args_list
        done_payloads = [c[0][0] for c in update_calls if c[0][0].get("status") == "done"]
        assert done_payloads, "Expected final flyer completion update"

        metadata = done_payloads[-1]["extraction_metadata"]
        expected_keys = {
            "extraction_started_at",
            "extraction_finished_at",
            "provider_seconds",
            "variant_expansion_seconds",
            "normalization_seconds",
            "dedupe_seconds",
            "product_upsert_seconds",
            "offer_insert_seconds",
            "total_seconds",
            "products_raw_count",
            "products_after_variants_count",
            "products_unique_count",
            "avg_format_bytes_compact",
            "avg_format_bytes_normalized",
            "chunk_size_pages",
            "chunks_total",
            "chunks_completed",
            "chunk_failures",
        }
        assert expected_keys.issubset(metadata.keys())
        assert metadata["avg_format_bytes_normalized"] >= metadata["avg_format_bytes_compact"]
        assert metadata["chunk_size_pages"] == 3
        assert metadata["chunks_total"] == 3
        assert metadata["chunks_completed"] == 3
        assert metadata["chunk_failures"] == 0
        assert metadata["extraction_started_at"].endswith("Z")
        assert metadata["extraction_finished_at"].endswith("Z")

    def test_chunk_progress_metadata_is_updated_after_each_provider_chunk(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )

        def _extract_products(
            file_bytes,
            mime_type,
            progress_callback=None,
            chunk_result_callback=None,
            start_chunk_index=1,
        ):
            if progress_callback:
                progress_callback(
                    {
                        "chunks_completed": 1,
                        "chunks_total": 3,
                        "current_chunk_start": 1,
                        "current_chunk_end": 3,
                        "pages_processed": 3,
                        "products_found": 4,
                    }
                )
                progress_callback(
                    {
                        "chunks_completed": 2,
                        "chunks_total": 3,
                        "current_chunk_start": 4,
                        "current_chunk_end": 6,
                        "pages_processed": 6,
                        "products_found": 7,
                    }
                )
            return (_EXTRACTED_PRODUCTS_V2, [])

        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = _extract_products

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=7),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        metadata_updates = [
            call[0][0]["extraction_metadata"]
            for call in sb.table.return_value.update.call_args_list
            if call[0][0].get("extraction_metadata", {}).get("stage") == "extracting"
        ]
        assert any(
            metadata.get("pages_processed") == 0
            and metadata.get("progress_percent") == 5
            and metadata.get("current_chunk_start") == 1
            and metadata.get("current_chunk_end") == 3
            for metadata in metadata_updates
        )
        assert any(
            metadata.get("pages_processed") == 3
            and metadata.get("progress_percent") == 43
            and metadata.get("current_chunk_start") == 1
            and metadata.get("current_chunk_end") == 3
            and metadata.get("products_found") == 4
            for metadata in metadata_updates
        )
        assert any(
            metadata.get("pages_processed") == 6
            and metadata.get("progress_percent") == 86
            and metadata.get("current_chunk_start") == 4
            and metadata.get("current_chunk_end") == 6
            and metadata.get("products_found") == 7
            for metadata in metadata_updates
        )

    def test_initial_extraction_metadata_includes_started_progress(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )
        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS_V2, [])

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=8),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        first_update = sb.table.return_value.update.call_args_list[0][0][0]
        metadata = first_update["extraction_metadata"]
        assert metadata["stage"] == "extracting"
        assert metadata["pages_total"] == 8
        assert metadata["extraction_started_at"].endswith("Z")
        assert metadata["pages_processed"] == 0
        assert metadata["progress_percent"] == 5
        assert metadata["current_chunk_start"] == 1
        assert metadata["current_chunk_end"] == 3

    def test_error_metadata_keeps_started_at_and_sets_finished_at(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )
        current_metadata = {
            "stage": "extracting",
            "extraction_started_at": "2026-05-07T12:00:00Z",
        }
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={"extraction_metadata": current_metadata}
        )
        mock_provider = MagicMock()
        mock_provider.extract_products.side_effect = RuntimeError("Gemini timeout")

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=8),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        error_updates = [
            call[0][0]
            for call in sb.table.return_value.update.call_args_list
            if call[0][0].get("status") == "error"
        ]
        assert error_updates
        metadata = error_updates[-1]["extraction_metadata"]
        assert metadata["extraction_started_at"] == "2026-05-07T12:00:00Z"
        assert metadata["extraction_finished_at"].endswith("Z")

    def test_incomplete_extraction_format_does_not_fail_entire_flyer(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (
            [
                {
                    "name": "Pasta Barilla",
                    "brand": "Barilla",
                    "category": "dispensa",
                    "format": {"tipo": "confezione_singola"},
                    "price_offer": 1.29,
                }
            ],
            [],
        )

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        update_calls = sb.table.return_value.update.call_args_list
        done_calls = [c for c in update_calls if c[0][0].get("status") == "done"]
        assert done_calls, "Expected flyer to complete despite incomplete format"

        # format lives on offers, not products
        offer_row = _offer_upsert_calls(sb)[0][0][0][0]
        assert offer_row["format"] == {"tipo": "confezione_singola"}
        assert offer_row["format_label"] == ""

    def test_provider_chunk_failure_is_exposed_in_error_message(self):
        sb = _make_sb(
            flyer_data={
                "id": "flyer-1",
                "file_url": "https://example.com/flyer.pdf",
                "file_name": "flyer.pdf",
                "supermarket_id": "sup-1",
                "supermarket_name": "Test Super",
                "valid_from": None,
                "valid_to": "2026-05-01",
                "user_id": "user-1",
            }
        )
        mock_provider = MagicMock()
        mock_provider.chunk_size_pages = 3
        mock_provider.extract_products.side_effect = ValueError(
            "Chunk 2/3 (pages 4-6) failed after 3 attempts"
        )

        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=7),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()

            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=mock_provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

        update_calls = sb.table.return_value.update.call_args_list
        error_payloads = [c[0][0] for c in update_calls if c[0][0].get("status") == "error"]
        assert error_payloads
        assert "Chunk 2/3 (pages 4-6) failed after 3 attempts" in error_payloads[-1]["error_message"]


class TestExtractionServicePushNotification:
    """notify_extraction_complete called on success and error; skipped when no user_id."""

    def _run_with_provider(self, sb: MagicMock, provider: MagicMock) -> None:
        with (
            patch("services.extraction.service.requests.get") as mock_get,
            patch("services.extraction.service.count_pdf_pages", return_value=1),
        ):
            mock_get.return_value.content = b"%PDF-fake"
            mock_get.return_value.raise_for_status = MagicMock()
            from services.extraction.service import ExtractionService
            svc = ExtractionService(provider=provider, supabase_factory=lambda: sb)
            svc.run("flyer-1")

    def test_notifies_on_success(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS, [])

        with patch("services.extraction.service.notify_extraction_complete") as mock_notify:
            self._run_with_provider(sb, mock_provider)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["success"] is True
        assert kwargs["flyer_id"] == "flyer-1"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["products_count"] >= 1

    def test_notifies_on_error(self):
        sb = _make_sb()
        mock_provider = MagicMock()
        mock_provider.extract_products.side_effect = RuntimeError("Gemini timeout")

        with patch("services.extraction.service.notify_extraction_complete") as mock_notify:
            self._run_with_provider(sb, mock_provider)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["flyer_id"] == "flyer-1"
        assert kwargs["user_id"] == "user-1"
        assert "Gemini timeout" in kwargs["error_message"]

    def test_skips_notify_without_user_id(self):
        flyer_data_no_user = {
            "id": "flyer-1",
            "file_url": "https://example.com/flyer.jpg",
            "file_name": "flyer.jpg",
            "supermarket_id": "sup-1",
            "supermarket_name": "Test Super",
            "valid_from": None,
            "valid_to": "2026-05-01",
            "user_id": None,
        }
        sb = _make_sb(flyer_data=flyer_data_no_user)
        mock_provider = MagicMock()
        mock_provider.extract_products.return_value = (_EXTRACTED_PRODUCTS, [])

        with patch("services.extraction.service.notify_extraction_complete") as mock_notify:
            self._run_with_provider(sb, mock_provider)

        mock_notify.assert_not_called()
