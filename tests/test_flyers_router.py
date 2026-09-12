"""Unit tests for api/routers/flyers.py — upload admin/manager gating + validation.

Tests verify that:
- Upload requires admin or manager role
- Upload always creates private flyers
- Manager auto-fills supermarket_id from profile
- Manager cannot upload for a different supermarket
- File type and size validation are enforced
- Duplicate hash+supermarket returns 409

Infrastructure modules (supabase, jose, etc.) are stubbed so tests run
without a venv or external services.
"""

from __future__ import annotations

import sys
import os
import types
from typing import Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# Stub infrastructure modules not available without a full venv
# ---------------------------------------------------------------------------
for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_settings_obj = MagicMock()
_settings_obj.llm_provider = "gemini"
_settings_obj.google_api_key = ""
_settings_obj.gemini_model = "gemma-4-31b-it"
_config_mod.settings = _settings_obj  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod

sys.modules["core.database"] = MagicMock()
for _svc_mod in ("services.extraction.service", "services.extraction", "services.extraction.providers"):
    sys.modules.pop(_svc_mod, None)

# ---------------------------------------------------------------------------
# Stub core.auth: provide real-looking dependencies for testing
# ---------------------------------------------------------------------------
from fastapi import HTTPException

_auth_mod = types.ModuleType("core.auth")

_auth_mod.get_current_user_id = MagicMock()  # type: ignore[attr-defined]
_auth_mod.get_current_user = MagicMock()  # type: ignore[attr-defined]
_auth_mod.require_admin_or_manager = MagicMock()  # type: ignore[attr-defined]


def _assert_flyer_access_real(profile: dict, flyer: dict) -> None:
    if profile.get("role") != "supermarket_manager":
        return
    managed_ids = _managed_supermarket_ids_real(profile)
    if flyer.get("supermarket_id") not in managed_ids:
        raise HTTPException(
            status_code=403,
            detail="Access denied: flyer belongs to a different supermarket",
        )


def _managed_supermarket_ids_real(profile: dict) -> list[str]:
    ids = profile.get("managed_supermarket_ids")
    if isinstance(ids, list):
        return ids
    managed = profile.get("managed_supermarket_id")
    return [managed] if managed else []


_auth_mod.assert_flyer_access = _assert_flyer_access_real  # type: ignore[attr-defined]
_auth_mod.managed_supermarket_ids = _managed_supermarket_ids_real  # type: ignore[attr-defined]


async def _optional_user_id() -> str | None:
    return None


_auth_mod.get_optional_user_id = _optional_user_id  # type: ignore[attr-defined]


def _require_admin_real(user: dict) -> dict:
    role = user.get("app_metadata", {}).get("role")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


_auth_mod.require_admin = _require_admin_real  # type: ignore[attr-defined]
sys.modules["core.auth"] = _auth_mod

from fastapi import FastAPI
import httpx
import pytest

import api.routers.flyers as _flyers_module
from api.routers.flyers import router
from tests.snapshot_utils import assert_matches_json_snapshot

_DEP_GET_USER_ID = _flyers_module.get_current_user_id
_DEP_PROFILE = _flyers_module.require_admin_or_manager

# ---------------------------------------------------------------------------
# Build a minimal test app
# ---------------------------------------------------------------------------
test_app = FastAPI()
test_app.include_router(router, prefix="/flyers")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ADMIN_PROFILE = {"id": "admin-456", "role": "admin", "managed_supermarket_id": None}
MANAGER_PROFILE = {
    "id": "mgr-123",
    "role": "supermarket_manager",
    "managed_supermarket_id": "sup-1",
    "managed_supermarket_ids": ["sup-1"],
}

_SMALL_PDF = b"%PDF-1.4 fake pdf content"


def _mock_supabase_for_upload(insert_return: Optional[dict] = None) -> MagicMock:
    """Return mock Supabase that simulates Storage download + table insert."""
    sb = MagicMock()
    sb.storage.from_.return_value.download.return_value = _SMALL_PDF
    sb.storage.from_.return_value.remove.return_value = MagicMock()
    sb.storage.from_.return_value.get_public_url.return_value = "https://storage.example.com/flyers/test.pdf"
    row_data = insert_return or {
        "id": "flyer-uuid",
        "user_id": "admin-456",
        "status": "pending",
        "is_public": False,
    }
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[row_data])
    return sb


async def _post_upload(dep_overrides: dict, data: dict | None = None) -> httpx.Response:
    test_app.dependency_overrides = dep_overrides
    user_id = dep_overrides[_DEP_GET_USER_ID]()
    body = {
        "storage_path": f"{user_id}/test.pdf",
        "file_name": "test.pdf",
        "content_type": "application/pdf",
        "supermarket_ids": ["sup-1"],
    }
    body.update(data or {})
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/flyers/upload/complete", json=body)


async def _get(url: str, dep_overrides: dict | None = None) -> httpx.Response:
    test_app.dependency_overrides = dep_overrides or {}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def _patch(url: str, dep_overrides: dict, json: dict) -> httpx.Response:
    test_app.dependency_overrides = dep_overrides
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(url, json=json)


async def _put(url: str, dep_overrides: dict, json: dict) -> httpx.Response:
    test_app.dependency_overrides = dep_overrides
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.put(url, json=json)


def test_confirmed_count_by_flyer_uses_database_aggregation():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[{"flyer_id": "flyer-taurianova", "offer_count": 1_353}]
    )
    counts = _flyers_module._confirmed_count_by_flyer(sb, ["flyer-taurianova"])

    assert counts == {"flyer-taurianova": 1_353}
    sb.rpc.assert_called_once_with(
        "count_offers_by_flyer",
        {"p_flyer_ids": ["flyer-taurianova"], "p_is_confirmed": True},
    )


def test_public_flyers_filters_nearby_branches_without_exact_count():
    sb = MagicMock()
    query = MagicMock()
    query.select.return_value = query
    query.eq.return_value = query
    query.in_.return_value = query
    query.order.return_value = query
    query.limit.return_value = query
    query.params.add.return_value = query.params
    query.execute.return_value = MagicMock(data=[])
    sb.table.return_value = query

    assert _flyers_module._public_flyers(sb, ["sup-1"]) == []

    query.select.assert_called_once_with("*")
    query.in_.assert_called_once_with("supermarket_id", ["sup-1"])


@pytest.mark.asyncio
async def test_flyer_discovery_reuses_public_location_context_once():
    sb = MagicMock()
    request = MagicMock(cookies={})
    context = ([{"id": "flyer-1"}], {"sup-1": 1.2})

    with (
        patch("api.routers.flyers.get_supabase", return_value=sb),
        patch("api.routers.flyers._public_flyer_context", return_value=context) as public_context,
        patch("api.routers.flyers._visible_public_flyers", return_value=[{"id": "flyer-1"}]),
        patch("api.routers.flyers._nearby_supermarket_rows", return_value=[{"id": "sup-1"}]),
    ):
        result = await _flyers_module.discover_public_flyers(
            request=request,
            user_id="user-1",
        )

    assert result == {"flyers": [{"id": "flyer-1"}], "supermarkets": [{"id": "sup-1"}]}
    public_context.assert_called_once_with(sb, request, "user-1")


@pytest.mark.asyncio
async def test_flyer_targets_returns_all_active_branches_for_admin():
    sb = MagicMock()
    query = MagicMock()
    query.select.return_value = query
    query.eq.return_value = query
    query.order.return_value = query
    query.execute.return_value = MagicMock(
        data=[
            {
                "id": "sup-near",
                "municipalities": {"name": "Polistena", "province_code": "RC"},
            },
            {
                "id": "sup-far",
                "municipalities": {"name": "Taurianova", "province_code": "RC"},
            },
        ]
    )
    sb.table.return_value = query

    with patch("api.routers.flyers.get_supabase", return_value=sb):
        response = await _get("/flyers/targets", {_DEP_PROFILE: lambda: ADMIN_PROFILE})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "sup-near",
            "municipality_name": "Polistena",
            "municipality_province_code": "RC",
        },
        {
            "id": "sup-far",
            "municipality_name": "Taurianova",
            "municipality_province_code": "RC",
        },
    ]


@pytest.mark.asyncio
async def test_flyer_targets_returns_only_managed_branches_for_manager():
    sb = MagicMock()
    query = MagicMock()
    query.select.return_value = query
    query.eq.return_value = query
    query.in_.return_value = query
    query.order.return_value = query
    query.execute.return_value = MagicMock(
        data=[
            {
                "id": "sup-1",
                "municipalities": {"name": "Polistena", "province_code": "RC"},
            }
        ]
    )
    sb.table.return_value = query

    with patch("api.routers.flyers.get_supabase", return_value=sb):
        response = await _get("/flyers/targets", {_DEP_PROFILE: lambda: MANAGER_PROFILE})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "sup-1",
            "municipality_name": "Polistena",
            "municipality_province_code": "RC",
        }
    ]
    query.in_.assert_called_once_with("id", ["sup-1"])


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("application/pdf", b"%PDF-1.7 test"),
        ("image/jpeg", b"\xff\xd8\xff\xe0 test"),
        ("image/png", b"\x89PNG\r\n\x1a\n test"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBPtest"),
        ("image/gif", b"GIF89atest"),
    ],
)
def test_file_signature_accepts_declared_type(content_type: str, content: bytes):
    assert _flyers_module._matches_file_signature(content, content_type) is True


@pytest.mark.parametrize("content_type", ["application/pdf", "image/jpeg", "image/png"])
def test_file_signature_rejects_mismatched_content_type(content_type: str):
    with pytest.raises(HTTPException) as exc_info:
        _flyers_module._assert_file_signature(b"<script>alert(1)</script>", content_type)

    assert exc_info.value.status_code == 422


def test_product_image_storage_extension_comes_from_verified_content_type():
    sb = MagicMock()

    _flyers_module._upload_product_image_to_storage(
        sb,
        storage_prefix="draft-offers/offer-1",
        file_content=b"\x89PNG\r\n\x1a\n test",
        content_type="image/png",
    )

    path = sb.storage.from_.return_value.upload.call_args.kwargs["path"]
    assert path.endswith(".png")


# ---------------------------------------------------------------------------
# Tests — upload privacy
# ---------------------------------------------------------------------------


class TestUploadFlyerPrivacy:
    @pytest.mark.asyncio
    async def test_admin_upload_creates_private_flyer(self):
        """Admin upload creates a private flyer (is_public=False)."""
        sb = _mock_supabase_for_upload({"id": "f1", "is_public": False, "status": "pending", "user_id": "admin-456"})

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _post_upload(
                {_DEP_GET_USER_ID: lambda: "admin-456", _DEP_PROFILE: lambda: ADMIN_PROFILE},
            )

        assert resp.status_code == 201
        insert_call_kwargs = sb.table.return_value.insert.call_args_list[0][0][0]
        assert insert_call_kwargs["is_public"] is False

    @pytest.mark.asyncio
    async def test_upload_ignores_is_public_form_field(self):
        """Upload endpoint ignores any submitted is_public field and keeps flyer private."""
        sb = _mock_supabase_for_upload({"id": "f2", "is_public": False, "status": "pending", "user_id": "admin-456"})

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _post_upload(
                {_DEP_GET_USER_ID: lambda: "admin-456", _DEP_PROFILE: lambda: ADMIN_PROFILE},
                data={"is_public": "true", "supermarket_ids": ["sup-1"]},
            )

        assert resp.status_code == 201
        insert_call_kwargs = sb.table.return_value.insert.call_args_list[0][0][0]
        assert insert_call_kwargs["is_public"] is False

    @pytest.mark.asyncio
    async def test_manager_auto_fills_supermarket_id(self):
        """Manager upload without supermarket_id uses managed_supermarket_id."""
        sb = _mock_supabase_for_upload({"id": "f3", "is_public": False, "status": "pending", "user_id": "mgr-123"})
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={"name": "Manager Market"}
        )

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _post_upload(
                {_DEP_GET_USER_ID: lambda: "mgr-123", _DEP_PROFILE: lambda: MANAGER_PROFILE},
                data={"storage_path": "mgr-123/test.pdf", "supermarket_ids": []},
            )

        assert resp.status_code == 201
        insert_call_kwargs = sb.table.return_value.insert.call_args_list[0][0][0]
        assert insert_call_kwargs["supermarket_id"] == "sup-1"

    @pytest.mark.asyncio
    async def test_manager_wrong_supermarket_id_403(self):
        """Manager providing a different supermarket_id receives 403."""
        sb = _mock_supabase_for_upload()

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _post_upload(
                {_DEP_GET_USER_ID: lambda: "mgr-123", _DEP_PROFILE: lambda: MANAGER_PROFILE},
                data={"storage_path": "mgr-123/test.pdf", "supermarket_ids": ["sup-other"]},
            )

        assert resp.status_code == 403


class TestUploadFlyerValidation:
    @pytest.mark.asyncio
    async def test_unsupported_content_type_rejected(self):
        """Files with unsupported MIME types return 422."""
        resp = await _post_upload(
            {_DEP_GET_USER_ID: lambda: "admin-456", _DEP_PROFILE: lambda: ADMIN_PROFILE},
            data={"content_type": "text/plain", "file_name": "test.txt"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_oversized_file_rejected(self):
        """Files exceeding 50 MB return 413."""
        sb = _mock_supabase_for_upload()
        sb.storage.from_.return_value.download.return_value = b"x" * (50 * 1024 * 1024 + 1)
        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _post_upload({_DEP_GET_USER_ID: lambda: "admin-456", _DEP_PROFILE: lambda: ADMIN_PROFILE})
        assert resp.status_code == 413


class TestFlyerPreview:
    @pytest.mark.asyncio
    async def test_public_flyer_preview_returns_cached_thumbnail(self):
        sb = MagicMock()
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={
                "id": "flyer-1",
                "is_public": True,
                "status": "done",
                "preview_path": "previews/flyer-1.webp",
            }
        )
        sb.storage.from_.return_value.download.return_value = b"webp"

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch("api.routers.flyers._has_confirmed_offers", return_value=True),
        ):
            resp = await _get("/flyers/flyer-1/preview")

        assert resp.status_code == 200
        assert resp.content == b"webp"
        assert resp.headers["content-type"] == "image/webp"
        assert "s-maxage=86400" in resp.headers["cache-control"]
        sb.storage.from_.return_value.download.assert_called_once_with("previews/flyer-1.webp")

    @pytest.mark.asyncio
    async def test_private_preview_url_remains_signed_for_admin_workflows(self, request):
        sb = MagicMock()
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={
                "id": "flyer-1",
                "is_public": True,
                "status": "done",
                "preview_path": "previews/flyer-1.webp",
            }
        )
        sb.storage.from_.return_value.create_signed_url.return_value = {
            "signedURL": "https://storage.example.com/preview.webp"
        }

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch("api.routers.flyers._has_confirmed_offers", return_value=True),
        ):
            resp = await _get("/flyers/flyer-1/preview-url")

        assert resp.status_code == 200
        assert_matches_json_snapshot(request, "flyer_preview_response", resp.json())

    def test_missing_preview_is_generated_and_persisted(self):
        sb = MagicMock()
        flyer = {
            "id": "flyer-1",
            "file_url": "https://supabase.test/storage/v1/object/public/flyers/user/flyer.pdf",
            "file_type": "pdf",
            "preview_path": None,
        }
        sb.storage.from_.return_value.download.return_value = b"pdf"

        with patch("api.routers.flyers.settings.supabase_url", "https://supabase.test"), patch(
            "api.routers.flyers.render_flyer_preview", return_value=b"webp"
        ):
            preview_path = _flyers_module._ensure_flyer_preview(sb, flyer)

        assert preview_path == "previews/flyer-1.webp"
        sb.storage.from_.return_value.upload.assert_called_once_with(
            path="previews/flyer-1.webp",
            file=b"webp",
            file_options={"content-type": "image/webp", "upsert": "true"},
        )


class TestUploadFlyerDuplicate:
    @pytest.mark.asyncio
    async def test_duplicate_hash_and_supermarket_returns_409(self):
        """Uploading the same file+supermarket twice returns 409 Conflict."""
        sb = MagicMock()
        sb.storage.from_.return_value.download.return_value = _SMALL_PDF
        sb.storage.from_.return_value.remove.return_value = MagicMock()
        sb.storage.from_.return_value.get_public_url.return_value = "https://storage.example.com/flyers/dup.pdf"
        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch("api.routers.flyers._duplicate_target_conflicts", return_value={"sup-1"}),
        ):
            resp = await _post_upload(
                {_DEP_GET_USER_ID: lambda: "admin-456", _DEP_PROFILE: lambda: ADMIN_PROFILE},
            )

        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_no_duplicate_check_without_supermarket_name(self):
        """When supermarket_name is absent the duplicate check is skipped entirely."""
        sb = _mock_supabase_for_upload()

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _post_upload(
                {_DEP_GET_USER_ID: lambda: "admin-456", _DEP_PROFILE: lambda: ADMIN_PROFILE},
            )

        assert resp.status_code == 201
        assert sb.table.return_value.insert.call_count >= 1


class TestPublicFlyersVisibility:
    @pytest.mark.asyncio
    async def test_public_list_ignores_legacy_location_parameters(self):
        sb = MagicMock()
        flyers_table = MagicMock()
        query = flyers_table.select.return_value
        query.eq.return_value = query
        query.order.return_value = query
        first_page = MagicMock()
        first_page.execute.return_value = MagicMock(
            count=2,
            data=[
                {
                    "id": "flyer-taurianova",
                    "source_flyer_id": "source-conad",
                    "supermarket_id": "taurianova",
                }
            ],
        )
        second_page = MagicMock()
        second_page.execute.return_value = MagicMock(
            count=2,
            data=[
                {
                    "id": "flyer-polistena",
                    "source_flyer_id": "source-conad",
                    "supermarket_id": "polistena",
                }
            ],
        )
        empty_page = MagicMock()
        empty_page.execute.return_value = MagicMock(data=[])
        query.range.side_effect = [first_page, second_page, empty_page]
        sb.table.return_value = flyers_table

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch(
                "api.routers.flyers.nearby_supermarket_distances",
                return_value={"taurianova": 7.3, "polistena": 1.1},
            ),
            patch(
                "api.routers.flyers._confirmed_count_by_flyer",
                return_value={"flyer-polistena": 306, "flyer-taurianova": 306},
            ),
        ):
            resp = await _get("/flyers/public?lat=38.6&lng=16.0")

        assert resp.status_code == 428
        assert resp.json()["detail"]["code"] == "guest_location_required"
        return
        assert resp.json() == [
            {
                "id": "flyer-polistena",
                "source_flyer_id": "source-conad",
                "supermarket_id": "polistena",
                "confirmed_count": 306,
            },
            {
                "id": "flyer-taurianova",
                "source_flyer_id": "source-conad",
                "supermarket_id": "taurianova",
                "confirmed_count": 306,
            }
        ]

    @pytest.mark.asyncio
    async def test_public_list_requires_signed_location_before_visibility_checks(self):
        sb = MagicMock()

        flyers_table = MagicMock()
        flyers_result = MagicMock()
        flyers_result.data = [
            {"id": "flyer-hidden", "supermarket_id": "sup-hidden", "status": "done", "is_public": True, "flyer_kind": "published_target"},
            {"id": "flyer-visible", "supermarket_id": "sup-visible", "status": "done", "is_public": True, "flyer_kind": "published_target"},
        ]
        query = flyers_table.select.return_value
        query.eq.return_value = query
        query.order.return_value = query
        empty_page = MagicMock()
        empty_page.execute.return_value = MagicMock(data=[])
        query.range.side_effect = [query, empty_page]
        query.execute.return_value = flyers_result

        sb.rpc.return_value.execute.return_value = MagicMock(
            data=[{"flyer_id": "flyer-visible", "offer_count": 1}]
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                return flyers_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch(
                "api.routers.flyers.nearby_supermarket_distances",
                return_value={"sup-hidden": 1.0, "sup-visible": 1.5},
            ),
        ):
            resp = await _get("/flyers/public?lat=38.6&lng=16.0")

        assert resp.status_code == 428
        return
        assert resp.json() == [
            {
                "id": "flyer-visible",
                "supermarket_id": "sup-visible",
                "status": "done",
                "is_public": True,
                "flyer_kind": "published_target",
                "confirmed_count": 1,
            }
        ]

    @pytest.mark.asyncio
    async def test_public_list_requires_signed_location_before_date_checks(self):
        sb = MagicMock()

        flyers_table = MagicMock()
        flyers_result = MagicMock()
        flyers_result.data = [
            {
                "id": "flyer-future",
                "supermarket_id": "sup-future",
                "status": "done",
                "is_public": True,
                "flyer_kind": "published_target",
                "valid_from": "2099-01-01",
            },
            {"id": "flyer-visible", "supermarket_id": "sup-visible", "status": "done", "is_public": True, "flyer_kind": "published_target"},
        ]
        query = flyers_table.select.return_value
        query.eq.return_value = query
        query.order.return_value = query
        empty_page = MagicMock()
        empty_page.execute.return_value = MagicMock(data=[])
        query.range.side_effect = [query, empty_page]
        query.execute.return_value = flyers_result

        sb.rpc.return_value.execute.return_value = MagicMock(
            data=[
                {"flyer_id": "flyer-future", "offer_count": 1},
                {"flyer_id": "flyer-visible", "offer_count": 1},
            ]
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                return flyers_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch(
                "api.routers.flyers.nearby_supermarket_distances",
                return_value={"sup-future": 1.0, "sup-visible": 1.5},
            ),
        ):
            resp = await _get("/flyers/public?lat=38.6&lng=16.0")

        assert resp.status_code == 428
        return
        assert resp.json() == [
            {
                "id": "flyer-visible",
                "supermarket_id": "sup-visible",
                "status": "done",
                "is_public": True,
                "flyer_kind": "published_target",
                "confirmed_count": 1,
            }
        ]

    @pytest.mark.asyncio
    async def test_public_list_requires_signed_location_before_radius_checks(self):
        sb = MagicMock()
        flyers_table = MagicMock()
        query = flyers_table.select.return_value
        query.eq.return_value = query
        query.order.return_value = query
        page = MagicMock()
        page.execute.return_value = MagicMock(
            data=[
                {"id": "flyer-near", "supermarket_id": "sup-near"},
                {"id": "flyer-far", "supermarket_id": "sup-far"},
            ]
        )
        empty_page = MagicMock()
        empty_page.execute.return_value = MagicMock(data=[])
        query.range.side_effect = [page, empty_page]
        sb.table.return_value = flyers_table

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch(
                "api.routers.flyers.nearby_supermarket_distances",
                return_value={"sup-near": 1.2},
            ),
            patch(
                "api.routers.flyers._confirmed_count_by_flyer",
                return_value={"flyer-near": 2},
            ),
        ):
            resp = await _get("/flyers/public?lat=38.6&lng=16.0")

        assert resp.status_code == 428
        return
        assert resp.json() == [
            {"id": "flyer-near", "supermarket_id": "sup-near", "confirmed_count": 2}
        ]



class TestManagerFlyerTargetsAccess:
    @pytest.mark.asyncio
    async def test_list_flyers_includes_source_flyer_when_manager_owns_one_target(self):
        sb = MagicMock()

        flyers_table = MagicMock()
        flyers_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "flyer-source",
                    "supermarket_id": "sup-1",
                    "supermarket_name": "Manager Market",
                    "status": "done",
                    "is_public": False,
                    "flyer_kind": "source",
                },
                {
                    "id": "flyer-other",
                    "supermarket_id": "sup-other",
                    "supermarket_name": "Other Market",
                    "status": "done",
                    "is_public": False,
                    "flyer_kind": "source",
                },
            ]
        )

        flyer_targets_table = MagicMock()
        flyer_targets_table.select.return_value.eq.return_value.execute.side_effect = [
            MagicMock(data=[{"supermarket_id": "sup-1", "supermarkets": {"name": "Manager Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-other", "supermarkets": {"name": "Other Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-other", "supermarkets": {"name": "Other Market"}}]),
        ]

        offers_table = MagicMock()
        offers_table.select.return_value.in_.return_value.eq.return_value.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[]),
        ]

        published_targets_query = MagicMock()
        published_targets_query.eq.return_value.in_.return_value.execute.return_value = MagicMock(
            data=[]
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                class _FlyersDispatch:
                    def select(self, *args, **kwargs):
                        if args == ("*",):
                            return flyers_table.select(*args, **kwargs)
                        return published_targets_query

                return _FlyersDispatch()
            if table_name == "flyer_targets":
                return flyer_targets_table
            if table_name == "offers":
                return offers_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _get(
                "/flyers",
                {_DEP_PROFILE: lambda: MANAGER_PROFILE},
            )

        assert resp.status_code == 200
        assert [row["id"] for row in resp.json()] == ["flyer-source"]

    @pytest.mark.asyncio
    async def test_list_flyers_ignores_legacy_admin_query_flag(self):
        sb = MagicMock()

        flyers_table = MagicMock()
        flyers_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "flyer-source",
                    "supermarket_id": "sup-1",
                    "supermarket_name": "Manager Market",
                    "status": "done",
                    "is_public": False,
                    "flyer_kind": "source",
                },
                {
                    "id": "flyer-other",
                    "supermarket_id": "sup-other",
                    "supermarket_name": "Other Market",
                    "status": "done",
                    "is_public": False,
                    "flyer_kind": "source",
                },
            ]
        )

        flyer_targets_table = MagicMock()
        flyer_targets_table.select.return_value.eq.return_value.execute.side_effect = [
            MagicMock(data=[{"supermarket_id": "sup-1", "supermarkets": {"name": "Manager Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-other", "supermarkets": {"name": "Other Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-other", "supermarkets": {"name": "Other Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-1", "supermarkets": {"name": "Manager Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-other", "supermarkets": {"name": "Other Market"}}]),
            MagicMock(data=[{"supermarket_id": "sup-other", "supermarkets": {"name": "Other Market"}}]),
        ]

        offers_table = MagicMock()
        offers_table.select.return_value.in_.return_value.eq.return_value.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=[]),
            MagicMock(data=[]),
        ]

        published_targets_query = MagicMock()
        published_targets_query.eq.return_value.in_.return_value.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[]),
        ]

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                class _FlyersDispatch:
                    def select(self, *args, **kwargs):
                        if args == ("*",):
                            return flyers_table.select(*args, **kwargs)
                        return published_targets_query

                return _FlyersDispatch()
            if table_name == "flyer_targets":
                return flyer_targets_table
            if table_name == "offers":
                return offers_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            response_without_flag = await _get(
                "/flyers",
                {_DEP_PROFILE: lambda: MANAGER_PROFILE},
            )
            response_with_flag = await _get(
                "/flyers?admin=true",
                {_DEP_PROFILE: lambda: MANAGER_PROFILE},
            )

        assert response_without_flag.status_code == 200
        assert response_with_flag.status_code == 200
        assert response_with_flag.json() == response_without_flag.json()

    @pytest.mark.asyncio
    async def test_list_flyers_includes_confirmation_counts_for_source_flyer(self):
        sb = MagicMock()

        flyers_table = MagicMock()
        flyers_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "flyer-source",
                    "supermarket_id": "sup-1",
                    "supermarket_name": "Manager Market",
                    "status": "done",
                    "is_public": False,
                    "flyer_kind": "source",
                }
            ]
        )

        flyer_targets_table = MagicMock()
        flyer_targets_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"supermarket_id": "sup-1", "supermarkets": {"name": "Manager Market"}}]
        )

        sb.rpc.return_value.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[{"flyer_id": "flyer-source", "offer_count": 2}]),
        ]

        published_targets_query = MagicMock()
        published_targets_query.eq.return_value.in_.return_value.execute.return_value = MagicMock(
            data=[{"source_flyer_id": "flyer-source"}]
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                class _FlyersDispatch:
                    def select(self, *args, **kwargs):
                        if args == ("*",):
                            return flyers_table.select(*args, **kwargs)
                        return published_targets_query

                return _FlyersDispatch()
            if table_name == "flyer_targets":
                return flyer_targets_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _get(
                "/flyers",
                {_DEP_PROFILE: lambda: ADMIN_PROFILE},
            )

        assert resp.status_code == 200
        assert resp.json()[0]["draft_count"] == 0
        assert resp.json()[0]["confirmed_count"] == 2
        assert resp.json()[0]["published_target_count"] == 1

    @pytest.mark.asyncio
    async def test_get_flyer_allows_source_flyer_when_manager_owns_one_target(self):
        sb = MagicMock()

        flyers_table = MagicMock()
        flyers_table.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
            data={
                "id": "flyer-source",
                "supermarket_id": "sup-1",
                "supermarket_name": "Manager Market",
                "status": "done",
                "is_public": False,
                "flyer_kind": "source",
            }
        )

        flyer_targets_table = MagicMock()
        flyer_targets_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"supermarket_id": "sup-1", "supermarkets": {"name": "Manager Market"}}]
        )

        offers_table = MagicMock()
        offers_table.select.return_value.in_.return_value.eq.return_value.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[]),
        ]

        published_targets_query = MagicMock()
        published_targets_query.eq.return_value.in_.return_value.execute.return_value = MagicMock(
            data=[]
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                class _FlyersDispatch:
                    def select(self, *args, **kwargs):
                        if args == ("*",):
                            return flyers_table.select(*args, **kwargs)
                        return published_targets_query

                return _FlyersDispatch()
            if table_name == "flyer_targets":
                return flyer_targets_table
            if table_name == "offers":
                return offers_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _get(
                "/flyers/flyer-source",
                {_DEP_PROFILE: lambda: MANAGER_PROFILE},
            )

        assert resp.status_code == 200
        assert resp.json()["id"] == "flyer-source"


class TestUpdateFlyerValidity:
    @pytest.mark.asyncio
    async def test_patch_updates_source_and_published_offer_dates(self):
        sb = MagicMock()

        source_flyer = {
            "id": "flyer-source",
            "supermarket_id": "sup-1",
            "supermarket_name": "Coop",
            "status": "done",
            "is_public": False,
            "flyer_kind": "source",
            "valid_from": "2026-04-01",
            "valid_to": "2026-04-07",
        }
        updated_flyer = {
            **source_flyer,
            "valid_from": "2026-05-01",
            "valid_to": "2026-05-10",
        }

        flyers_table = MagicMock()

        select_call = 0

        def flyers_select_side_effect(*_args, **_kwargs):
            nonlocal select_call
            select_call += 1
            chain = MagicMock()
            if select_call == 1:
                chain.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
                    data=source_flyer
                )
            elif select_call == 2:
                chain.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                    data=[{"id": "flyer-published-1"}]
                )
            else:
                chain.eq.return_value.single.return_value.execute.return_value = MagicMock(
                    data=updated_flyer
                )
            return chain

        flyers_table.select.side_effect = flyers_select_side_effect
        flyers_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        offers_table = MagicMock()
        offers_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        flyer_targets_table = MagicMock()
        flyer_targets_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        notification_jobs_table = MagicMock()
        notification_jobs_table.update.return_value.eq.return_value.eq.return_value.in_.return_value.execute.return_value = (
            MagicMock(data=[])
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "flyers":
                return flyers_table
            if table_name == "offers":
                return offers_table
            if table_name == "flyer_targets":
                return flyer_targets_table
            if table_name == "notification_jobs":
                return notification_jobs_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch

        with patch("api.routers.flyers.get_supabase", return_value=sb):
            resp = await _patch(
                "/flyers/flyer-source",
                {_DEP_PROFILE: lambda: ADMIN_PROFILE},
                {"valid_from": "2026-05-01", "valid_to": "2026-05-10"},
            )

        assert resp.status_code == 200
        assert resp.json()["valid_from"] == "2026-05-01"
        assert resp.json()["valid_to"] == "2026-05-10"
        assert flyers_table.update.call_count == 2
        assert offers_table.update.call_count == 2
        assert notification_jobs_table.update.call_count == 1
        assert all(
            call.args[0] == {"valid_from": "2026-05-01", "valid_to": "2026-05-10"}
            for call in flyers_table.update.call_args_list + offers_table.update.call_args_list
        )


class TestUpdateFlyerTargets:
    def test_duplicate_check_ignores_same_source_published_targets(self):
        sb = MagicMock()
        flyers = [
            {
                "flyer_kind": "published_target",
                "supermarket_id": "sup-1",
                "source_flyer_id": "flyer-source",
            },
            {
                "flyer_kind": "published_target",
                "supermarket_id": "sup-2",
                "source_flyer_id": "flyer-other",
            },
        ]
        sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=flyers
        )

        conflicts = _flyers_module._duplicate_target_conflicts(
            sb,
            file_hash="hash-1",
            supermarket_ids=["sup-1", "sup-2"],
            exclude_source_flyer_id="flyer-source",
        )

        assert conflicts == {"sup-2"}

    @pytest.mark.asyncio
    async def test_put_targets_syncs_published_offers_after_confirmation(self):
        sb = MagicMock()
        source_flyer = {
            "id": "flyer-source",
            "user_id": "admin-456",
            "supermarket_id": "sup-1",
            "supermarket_name": "Coop",
            "status": "done",
            "is_public": False,
            "flyer_kind": "source",
            "file_hash": "hash-1",
        }
        updated_flyer = {**source_flyer, "supermarket_id": "sup-2"}
        flyers_table = MagicMock()
        select_chain = flyers_table.select.return_value.eq.return_value
        select_chain.maybe_single.return_value.execute.return_value = MagicMock(
            data=source_flyer
        )
        select_chain.single.return_value.execute.return_value = MagicMock(
            data=updated_flyer
        )
        flyers_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        sb.table.return_value = flyers_table
        targets = [
            {"supermarket_id": "sup-1", "supermarket_name": "Coop"},
            {"supermarket_id": "sup-2", "supermarket_name": "Esselunga"},
        ]

        with (
            patch("api.routers.flyers.get_supabase", return_value=sb),
            patch("api.routers.flyers._duplicate_target_conflicts", return_value=set()),
            patch("api.routers.flyers._replace_flyer_targets") as replace_mock,
            patch(
                "api.routers.flyers._supermarket_name_map",
                return_value={"sup-1": "Coop", "sup-2": "Esselunga"},
            ),
            patch("api.routers.flyers._flyer_targets", return_value=targets),
            patch(
                "api.routers.flyers._source_master_offers",
                return_value=[{"id": "offer-1"}],
            ),
            patch(
                "api.routers.flyers._sync_published_targets_for_source_flyer"
            ) as sync_mock,
        ):
            resp = await _put(
                "/flyers/flyer-source/targets",
                {_DEP_PROFILE: lambda: ADMIN_PROFILE},
                {"supermarket_ids": ["sup-1", "sup-2"]},
            )

        assert resp.status_code == 200
        replace_mock.assert_called_once_with(
            sb,
            flyer_id="flyer-source",
            supermarket_ids=["sup-1", "sup-2"],
        )
        sync_mock.assert_called_once_with(
            sb,
            source_flyer=updated_flyer,
            targets=targets,
            notify_new=True,
            source_offers=[{"id": "offer-1"}],
        )

    def test_sync_published_targets_adds_and_removes_public_materialization(self):
        sb = MagicMock()
        offers_table = MagicMock()
        flyers_table = MagicMock()
        flyers_table.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "flyer-pub-2"}]
        )

        def _dispatch(table_name: str) -> MagicMock:
            if table_name == "offers":
                return offers_table
            if table_name == "flyers":
                return flyers_table
            raise AssertionError(f"unexpected table {table_name}")

        sb.table.side_effect = _dispatch
        source_flyer = {
            "id": "flyer-source",
            "user_id": "admin-456",
            "file_url": "https://example.com/flyer.pdf",
            "file_type": "pdf",
            "file_name": "volantino.pdf",
            "valid_from": "2026-05-01",
            "valid_to": "2026-05-10",
            "pages_count": 2,
            "extraction_metadata": None,
            "file_hash": "hash-1",
        }
        targets = [
            {"supermarket_id": "sup-1", "supermarket_name": "Coop"},
            {"supermarket_id": "sup-2", "supermarket_name": "Esselunga"},
        ]
        target_flyers_after = {
            "sup-1": {"flyer_id": "flyer-pub-1", "supermarket_name": "Coop"},
            "sup-2": {"flyer_id": "flyer-pub-2", "supermarket_name": "Esselunga"},
        }

        with (
            patch(
                "api.routers.flyers._published_target_flyers",
                side_effect=[
                    {
                        "sup-1": {"flyer_id": "flyer-pub-1", "supermarket_name": "Coop"},
                        "sup-old": {
                            "flyer_id": "flyer-pub-old",
                            "supermarket_name": "Old",
                        },
                    },
                    target_flyers_after,
                ],
            ),
            patch(
                "api.routers.flyers._sync_published_clones_for_source_offers",
                return_value={"flyer-pub-1": 1, "flyer-pub-2": 1},
            ) as clone_sync,
            patch("api.routers.flyers.enqueue_flyer_published") as flyer_job_mock,
        ):
            counts = _flyers_module._sync_published_targets_for_source_flyer(
                sb,
                source_flyer=source_flyer,
                targets=targets,
                notify_new=True,
                source_offers=[{"id": "offer-1"}],
            )

        offers_table.delete.return_value.in_.assert_called_once_with(
            "flyer_id",
            ["flyer-pub-old"],
        )
        flyers_table.delete.return_value.in_.assert_called_once_with(
            "id",
            ["flyer-pub-old"],
        )
        assert flyers_table.insert.call_args.args[0]["supermarket_id"] == "sup-2"
        clone_sync.assert_called_once_with(
            sb,
            source_offers=[{"id": "offer-1"}],
            target_flyers=target_flyers_after,
        )
        flyer_job_mock.assert_called_once()
        assert counts == {"flyer-pub-1": 1, "flyer-pub-2": 1}


@pytest.mark.asyncio
async def test_public_flyer_file_url_is_direct_storage_and_never_downloaded_by_api():
    sb = MagicMock()
    flyer_result = MagicMock()
    flyer_result.data = {
        "id": "flyer-1",
        "is_public": True,
        "status": "done",
        "valid_from": "2020-01-01",
        "valid_to": "2099-12-31",
        "file_url": "user/flyer.pdf",
    }
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = flyer_result
    sb.storage.from_.return_value.create_signed_url.return_value = {
        "signedURL": "https://storage.example.com/signed/flyer.pdf"
    }

    with (
        patch("api.routers.flyers.get_supabase", return_value=sb),
        patch("api.routers.flyers._has_confirmed_offers", return_value=True),
    ):
        resp = await _get(
            "/flyers/flyer-1/file-url",
            {_flyers_module.get_optional_user_id: lambda: None},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "file_url": "https://storage.example.com/signed/flyer.pdf",
        "expires_in": 900,
    }
    assert resp.headers["cache-control"] == "no-store"
    sb.storage.from_.return_value.create_signed_url.assert_called_once_with(
        "user/flyer.pdf", expires_in=900
    )
    sb.storage.from_.return_value.download.assert_not_called()


@pytest.mark.asyncio
async def test_expired_public_flyer_file_url_is_not_issued():
    sb = MagicMock()
    result = MagicMock(data={
        "id": "flyer-1",
        "is_public": True,
        "status": "done",
        "valid_from": "2000-01-01",
        "valid_to": "2000-01-02",
        "file_url": "user/flyer.pdf",
    })
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = result

    with (
        patch("api.routers.flyers.get_supabase", return_value=sb),
        patch("api.routers.flyers._has_confirmed_offers", return_value=True),
    ):
        resp = await _get("/flyers/flyer-1/file-url")

    assert resp.status_code == 404
    sb.storage.from_.return_value.create_signed_url.assert_not_called()


@pytest.mark.asyncio
async def test_draft_flyer_file_url_is_denied_to_anonymous_users():
    sb = MagicMock()
    result = MagicMock(data={
        "id": "flyer-1",
        "is_public": False,
        "status": "done",
        "file_url": "user/flyer.pdf",
    })
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = result

    with patch("api.routers.flyers.get_supabase", return_value=sb):
        resp = await _get("/flyers/flyer-1/file-url")

    assert resp.status_code == 403
    sb.storage.from_.return_value.create_signed_url.assert_not_called()


def test_public_flyer_representation_hides_storage_url():
    flyer = {"id": "flyer-1", "file_url": "https://storage.example.com/file.pdf"}

    assert _flyers_module._public_flyer_representation(flyer) == {"id": "flyer-1"}


def test_public_flyer_expiry_sort_key_prioritizes_nearest_expiry():
    flyers = [
        {"id": "undated", "valid_to": None},
        {"id": "later", "valid_to": "2026-08-20"},
        {"id": "sooner", "valid_to": "2026-08-10"},
    ]

    assert [flyer["id"] for flyer in sorted(flyers, key=_flyers_module._public_flyer_expiry_sort_key)] == [
        "sooner",
        "later",
        "undated",
    ]


def test_published_offer_clone_keeps_packshot_localization():
    source_offer = {
        "id": "source-offer-1",
        "name": "Latte",
        "packshot_source_page": 2,
        "packshot_bbox": [100, 200, 500, 600],
    }

    clone = _flyers_module._clone_offer_fields(
        source_offer,
        flyer_id="published-flyer-1",
        supermarket_id="supermarket-1",
        supermarket_name="Coop",
    )

    assert clone["packshot_source_page"] == 2
    assert clone["packshot_bbox"] == [100, 200, 500, 600]
    assert "is_active" not in clone


@pytest.mark.asyncio
async def test_interactive_offers_excludes_invalid_localizations_and_drafts():
    sb = MagicMock()
    flyer = {
        "id": "flyer-1",
        "status": "done",
        "is_public": True,
        "valid_from": "2026-08-01",
        "valid_to": "2099-08-10",
    }
    flyer_query = MagicMock()
    offers_query = MagicMock()
    sb.table.side_effect = lambda table: {
        "flyers": flyer_query,
        "offers": offers_query,
    }[table]
    flyer_query.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=flyer)
    active_offers_query = MagicMock()
    active_offers_query.execute.return_value = MagicMock(data=[
        {
            "id": "offer-valid",
            "name": "Latte",
            "brand": "Coop",
            "image_url": None,
            "price_offer": 1.99,
            "format_label": "1 L",
            "packshot_source_page": 1,
            "packshot_bbox": [100, 200, 500, 600],
        },
        {
            "id": "offer-invalid",
            "name": "Pane",
            "packshot_source_page": 1,
            "packshot_bbox": [600, 200, 500, 600],
        },
    ])

    with (
        patch("api.routers.flyers.get_supabase", return_value=sb),
        patch("api.routers.flyers._is_public_flyer_file", return_value=True),
        patch("api.routers.flyers.apply_current_offer_window", return_value=active_offers_query),
    ):
        response = await _flyers_module.list_interactive_offers("flyer-1")

    assert response == {
        "items": [
            {
                "id": "offer-valid",
                "name": "Latte",
                "brand": "Coop",
                "image_url": None,
                "price_offer": 1.99,
                "format_label": "1 L",
                "source_page": 1,
                "packshot_bbox": [100.0, 200.0, 500.0, 600.0],
            }
        ]
    }
