from __future__ import annotations

import io
import os
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# Stub infrastructure modules
# ---------------------------------------------------------------------------
for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_db_mod = types.ModuleType("core.database")
_db_mod.get_supabase = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.database"] = _db_mod

# ---------------------------------------------------------------------------
# Stub core.auth — use MagicMock so FastAPI doesn't infer body params
# ---------------------------------------------------------------------------
_auth_mod = types.ModuleType("core.auth")
_auth_mod.get_current_user = MagicMock()  # type: ignore[attr-defined]
_auth_mod.require_admin = MagicMock()  # type: ignore[attr-defined]


async def _optional_user_id() -> str | None:
    return None


_auth_mod.get_optional_user_id = _optional_user_id  # type: ignore[attr-defined]
sys.modules["core.auth"] = _auth_mod

from fastapi import FastAPI, HTTPException
import httpx
import pytest

import api.routers.supermarkets as _sm_module
from api.routers._nearby_supermarkets import DiscoveryArea
from api.routers.supermarkets import router

_DEP_REQUIRE_ADMIN = _sm_module.require_admin

test_app = FastAPI()
test_app.include_router(router, prefix="/supermarkets")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ADMIN_USER = {"id": "admin-1", "app_metadata": {"role": "admin"}}

MINIMAL_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"  # valid JPEG magic


@pytest.fixture(autouse=True)
def municipality_catalog(monkeypatch):
    monkeypatch.setattr(
        _sm_module,
        "get_municipality",
        lambda _sb, _code: {"code": "015146"},
    )


def _admin_dep():
    return ADMIN_USER


def _deny_dep():
    raise HTTPException(status_code=403, detail="Admin access required")


def _logo_file(content: bytes = MINIMAL_JPEG, mime: str = "image/jpeg") -> tuple:
    return ("logo.jpg", io.BytesIO(content), mime)


async def _get(url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def _post_admin_form(url: str, data: dict, logo: tuple | None = None) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_REQUIRE_ADMIN: _admin_dep}
    files = {"logo": logo or _logo_file()}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, data=data, files=files)


async def _post_form_denied(url: str, data: dict) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_REQUIRE_ADMIN: _deny_dep}
    files = {"logo": _logo_file()}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, data=data, files=files)


async def _patch_logo(url: str, logo: tuple | None = None) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_REQUIRE_ADMIN: _admin_dep}
    files = {"logo": logo or _logo_file()}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(url, files=files)


async def _patch_logo_denied(url: str) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_REQUIRE_ADMIN: _deny_dep}
    files = {"logo": _logo_file()}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(url, files=files)


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_location_parameters_do_not_filter_guest_supermarkets():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[{"id": "sm-1", "distance_km": 1.2}]
    )
    sb.table.return_value.select.return_value.in_.return_value.execute.return_value = MagicMock(
        data=[{"id": "sm-1", "name": "Lidl", "is_active": True}]
    )

    resp = await _get("/supermarkets?lat=45.464&lng=9.189&max_distance_km=10")

    assert resp.status_code == 428
    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_guest_supermarkets_require_signed_location():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[
            {"id": "sup-taurianova", "distance_km": 7.3},
            {"id": "sup-polistena", "distance_km": 1.1},
        ]
    )
    query = MagicMock()
    query.eq.return_value = query
    query.in_.return_value = query
    query.execute.return_value = MagicMock(
        data=[
            {"id": "sup-taurianova", "name": "Conad", "offers": [{"id": "offer-1"}]},
            {"id": "sup-polistena", "name": "Conad", "offers": [{"id": "offer-2"}]},
        ]
    )
    sb.table.return_value.select.return_value = query

    resp = await _get("/supermarkets?with_active_offers=true&lat=38.4&lng=16.1&max_distance_km=10")

    assert resp.status_code == 428


@pytest.mark.asyncio
async def test_with_active_offers_uses_authenticated_profile_radius():
    sb = MagicMock()
    with (
        patch("api.routers.supermarkets.get_supabase", return_value=sb),
        patch(
            "api.routers.supermarkets.request_location",
            return_value=DiscoveryArea("080061", 7.0),
        ) as request_location,
        patch(
            "api.routers.supermarkets.nearby_supermarket_distances",
            return_value={"sup-polistena": 1.1},
        ),
            patch(
                "api.routers.supermarkets.active_nearby_supermarkets",
                return_value=[
                    {"id": "sup-polistena", "name": "Conad", "distance_km": 1.1}
                ],
            ),
    ):
        result = await _sm_module.list_supermarkets(
            with_active_offers=True,
            user_id="user-1",
        )

    request_location.assert_called_once_with(sb, "user-1", None)
    assert result == [{"id": "sup-polistena", "name": "Conad", "distance_km": 1.1}]


@pytest.mark.asyncio
async def test_authenticated_supermarkets_without_location_are_not_global():
    sb = MagicMock()
    with (
        patch("api.routers.supermarkets.get_supabase", return_value=sb),
        patch("api.routers.supermarkets.request_location", return_value=None),
    ):
        result = await _sm_module.list_supermarkets(user_id="manager-1")

    assert result == []
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_location_parameters_cannot_include_deep_link_supermarket():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(data=[])
    sb.table.return_value.select.return_value.in_.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "sm-far", "name": "Conad", "is_active": True}]
    )

    resp = await _get(
        "/supermarkets?lat=45.464&lng=9.189&max_distance_km=10&include_ids=sm-far"
    )

    assert resp.status_code == 428


@pytest.mark.asyncio
async def test_admin_supermarkets_use_authenticated_profile_distance():
    sb = MagicMock()
    query = MagicMock()
    query.select.return_value = query
    query.in_.return_value = query
    query.execute.return_value = MagicMock(
        data=[
                {
                    "id": "sm-near",
                    "name": "Diper",
                    "municipality_code": "080061",
                    "municipalities": {"name": "Polistena", "province_code": "RC"},
                },
                {
                    "id": "sm-far",
                    "name": "Diper",
                    "municipality_code": "080050",
                    "municipalities": {"name": "Gioia Tauro", "province_code": "RC"},
                },
        ]
    )
    sb.table.return_value = query

    with (
        patch("api.routers.supermarkets.get_supabase", return_value=sb),
        patch(
            "api.routers.supermarkets.request_location",
            return_value=DiscoveryArea("080061", 7.0),
        ) as request_location,
        patch(
            "api.routers.supermarkets.nearby_supermarket_distances",
            return_value={"sm-near": 1.1},
        ),
    ):
        result = await _sm_module.list_supermarkets(
            with_active_offers=False,
            user_id="admin-1",
            request=MagicMock(),
        )

    request_location.assert_called_once_with(sb, "admin-1", None)
    assert result == [
        {
            "id": "sm-near",
            "name": "Diper",
            "municipality_code": "080061",
            "municipality_name": "Polistena",
            "municipality_province_code": "RC",
            "distance_km": 1.1,
        }
    ]


# ---------------------------------------------------------------------------
# POST /supermarkets tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_supermarket_requires_admin():
    resp = await _post_form_denied("/supermarkets", {"name": "Nuovo Market"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_supermarket_success():
    new_row = {
        "id": "sm-new",
        "name": "Nuovo Market",
        "slug": "nuovo-market",
        "municipality_code": "015146",
        "is_active": True,
        "logo_url": None,
    }
    updated_row = {**new_row, "logo_url": "https://example.com/logos/sm-new.jpg"}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[new_row])
    sb.storage.from_.return_value.upload.return_value = None
    sb.storage.from_.return_value.get_public_url.return_value = "https://example.com/logos/sm-new.jpg"
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[updated_row])

    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _post_admin_form(
            "/supermarkets",
            {
                "name": "Nuovo Market",
                "municipality_code": "015146",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Nuovo Market"
    assert data["logo_url"] == "https://example.com/logos/sm-new.jpg"
    uploaded_options = sb.storage.from_.return_value.upload.call_args.kwargs[
        "file_options"
    ]
    assert uploaded_options["cache-control"] == "31536000"


@pytest.mark.asyncio
async def test_create_supermarket_uses_only_municipality_code():
    new_row = {"id": "sm-2", "name": "Test", "slug": "test", "municipality_code": "001272", "is_active": True, "logo_url": None}
    updated_row = {**new_row, "logo_url": "https://example.com/logos/sm-2.jpg"}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[new_row])
    sb.storage.from_.return_value.upload.return_value = None
    sb.storage.from_.return_value.get_public_url.return_value = "https://example.com/logos/sm-2.jpg"
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[updated_row])

    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _post_admin_form(
            "/supermarkets",
            {"name": "Test", "municipality_code": "001272", "address": "Via Po 5", "lat": 45.5, "lng": 9.2},
        )

    assert resp.status_code == 201
    inserted = sb.table.return_value.insert.call_args.args[0]
    assert inserted == {
        "name": "Test", "slug": "test", "municipality_code": "001272", "is_active": True,
    }


# ---------------------------------------------------------------------------
# Logo validation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_supermarket_logo_wrong_type_rejected():
    resp = await _post_admin_form(
        "/supermarkets",
        {"name": "Test", "municipality_code": "015146"},
        logo=("logo.pdf", io.BytesIO(b"%PDF"), "application/pdf"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_supermarket_logo_too_large_rejected():
    big_content = b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024 + 1)
    resp = await _post_admin_form(
        "/supermarkets",
        {"name": "Test", "municipality_code": "015146"},
        logo=("logo.jpg", io.BytesIO(big_content), "image/jpeg"),
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_create_supermarket_logo_upload_failure_rolls_back():
    new_row = {"id": "sm-fail", "name": "Rollback", "slug": "rollback", "is_active": True, "logo_url": None}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[new_row])
    sb.storage.from_.return_value.upload.side_effect = Exception("Storage down")

    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _post_admin_form(
            "/supermarkets", {"name": "Rollback", "municipality_code": "015146"}
        )

    assert resp.status_code == 500
    sb.table.return_value.delete.return_value.eq.return_value.execute.assert_called_once()


# ---------------------------------------------------------------------------
# PATCH /supermarkets/{id}/logo tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_logo_requires_admin():
    resp = await _patch_logo_denied("/supermarkets/sm-1/logo")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_logo_not_found():
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)

    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_logo("/supermarkets/nonexistent/logo")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_logo_wrong_type_rejected():
    resp = await _patch_logo(
        "/supermarkets/sm-1/logo",
        logo=("logo.gif", io.BytesIO(b"GIF89a"), "image/gif"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_logo_too_large_rejected():
    big_content = b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024 + 1)
    resp = await _patch_logo(
        "/supermarkets/sm-1/logo",
        logo=("logo.jpg", io.BytesIO(big_content), "image/jpeg"),
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_update_logo_success():
    existing = {"id": "sm-1", "logo_url": None}
    updated_row = {"id": "sm-1", "name": "Test", "logo_url": "https://example.com/logos/sm-1.jpg"}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=existing)
    sb.storage.from_.return_value.upload.return_value = None
    sb.storage.from_.return_value.get_public_url.return_value = "https://example.com/logos/sm-1.jpg"
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[updated_row])

    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_logo("/supermarkets/sm-1/logo")

    assert resp.status_code == 200
    assert resp.json()["logo_url"] == "https://example.com/logos/sm-1.jpg"


@pytest.mark.asyncio
async def test_update_logo_keeps_old_immutable_asset_when_replaced():
    old_url = "https://proj.supabase.co/storage/v1/object/public/logos/sm-1.jpg"
    existing = {"id": "sm-1", "logo_url": old_url}
    updated_row = {"id": "sm-1", "logo_url": "https://proj.supabase.co/storage/v1/object/public/logos/sm-1.png"}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=existing)
    sb.storage.from_.return_value.upload.return_value = None
    sb.storage.from_.return_value.get_public_url.return_value = updated_row["logo_url"]
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[updated_row])

    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_logo(
            "/supermarkets/sm-1/logo",
            logo=("logo.png", io.BytesIO(b"\x89PNG"), "image/png"),
        )

    assert resp.status_code == 200
    sb.storage.from_.return_value.remove.assert_not_called()


# ---------------------------------------------------------------------------
# PATCH /supermarkets/{id} (info update) helpers
# ---------------------------------------------------------------------------

async def _patch_info(url: str, data: dict) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_REQUIRE_ADMIN: _admin_dep}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(url, json=data)


async def _patch_info_denied(url: str, data: dict) -> httpx.Response:
    test_app.dependency_overrides = {_DEP_REQUIRE_ADMIN: _deny_dep}
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.patch(url, json=data)


# ---------------------------------------------------------------------------
# PATCH /supermarkets/{id} (info update) tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_supermarket_requires_admin():
    resp = await _patch_info_denied("/supermarkets/sm-1", {"name": "Nuovo Nome"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_supermarket_not_found():
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=None)
    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_info("/supermarkets/nonexistent", {"name": "X"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_supermarket_success():
    existing = {"id": "sm-1"}
    updated_row = {"id": "sm-1", "name": "Nuovo Nome", "municipality_code": "058091", "logo_url": None}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=existing)
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[updated_row])
    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_info("/supermarkets/sm-1", {"name": "Nuovo Nome", "municipality_code": "058091"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Nuovo Nome"


@pytest.mark.asyncio
async def test_update_supermarket_empty_body_returns_current():
    current_row = {"id": "sm-1", "name": "Esistente", "logo_url": None}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=current_row)
    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_info("/supermarkets/sm-1", {})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Esistente"


@pytest.mark.asyncio
async def test_update_supermarket_changes_municipality_code():
    existing = {"id": "sm-1", "municipality_code": "080061"}
    updated_row = {"id": "sm-1", "name": "Test", "municipality_code": "058091"}
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(data=existing)
    sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[updated_row])
    with patch("api.routers.supermarkets.get_supabase", return_value=sb):
        resp = await _patch_info("/supermarkets/sm-1", {"municipality_code": "058091"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_supermarket_rejects_client_coordinates():
    response = await _patch_info("/supermarkets/sm-1", {"lat": 41.9})

    assert response.status_code == 422
