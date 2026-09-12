"""Unit tests for manual offer creation in api/routers/offers.py."""

from __future__ import annotations

import sys
import os
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# Stub infrastructure modules
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
sys.modules.pop("core.auth", None)

from fastapi import FastAPI
import httpx
import pytest

import api.routers.offers as _offers_module
from api.routers._nearby_supermarkets import request_location
from api.routers.offers import router

_DEP_PROFILE = _offers_module.require_admin_or_manager

test_app = FastAPI()
test_app.include_router(router, prefix="/offers")

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
ADMIN_PROFILE = {"id": "admin-1", "role": "admin", "managed_supermarket_id": None}
MANAGER_PROFILE = {"id": "mgr-1", "role": "supermarket_manager", "managed_supermarket_id": "sup-1"}
MANAGER_OTHER_PROFILE = {"id": "mgr-2", "role": "supermarket_manager", "managed_supermarket_id": "sup-other"}

VALID_PAYLOAD = {
    "supermarket_id": "sup-1",
    "name": "Mozzarella",
    "brand": "Galbani",
    "price_offer": 1.99,
}


async def _post(url: str, dep_overrides: dict, json: dict | None = None) -> httpx.Response:
    test_app.dependency_overrides = dep_overrides
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json=json)


async def _get(url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


def test_authenticated_request_location_uses_profile_municipality_and_radius():
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = MagicMock(
        data={
            "municipality_code": "080061",
            "max_distance_km": 7,
        }
    )

    location = request_location(sb, "user-1", None)

    assert location.municipality_code == "080061"
    assert location.max_distance_km == 7.0


def test_guest_request_location_uses_signed_cookie_location():
    location = request_location(MagicMock(), None, ("080061", 10.0))

    assert location is not None
    assert location.municipality_code == "080061"
    assert location.max_distance_km == 10.0


def _make_sb(supermarket_data: dict | None = None) -> MagicMock:
    """Build a Supabase mock for the offers router."""
    sb = MagicMock()

    # supermarket lookup
    sm_result = MagicMock()
    sm_result.data = supermarket_data if supermarket_data is not None else {"id": "sup-1", "name": "Coop"}
    sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = sm_result

    # offer insert
    insert_result = MagicMock()
    insert_result.data = [{"id": "offer-new"}]
    sb.table.return_value.insert.return_value.execute.return_value = insert_result

    # offer select after insert
    final_result = MagicMock()
    final_result.data = {
        "id": "offer-new",
        "flyer_id": None,
        "supermarket_id": "sup-1",
        "supermarket_name": "Coop",
        "name": "Mozzarella",
        "brand": "Galbani",
        "category": None,
        "subcategory": None,
        "format": {},
        "format_label": "",
        "image_url": None,
        "price_offer": 1.99,
        "price_original": None,
        "unit_price": None,
        "unit_price_value": None,
        "unit_price_unit": None,
        "offer_notes": None,
        "valid_from": None,
        "valid_to": None,
        "is_confirmed": False,
    }
    sb.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = final_result

    return sb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_public_offers_ignores_legacy_location_parameters():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[{"id": "sup-1", "distance_km": 1.2}]
    )
    query = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    query.in_.return_value.order.return_value.execute.return_value = MagicMock(data=[])

    resp = await _get("/offers?lat=45.464&lng=9.189&max_distance_km=10")

    assert resp.status_code == 428
    assert resp.json()["detail"]["code"] == "guest_location_required"
    sb.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_list_public_offers_requires_guest_location():
    sb = MagicMock()
    query = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    query.order.return_value.range.return_value.execute.return_value = MagicMock(
        data=[
            {**_published_offer(offer_id="one", supermarket_id="sup-1", source_offer_id="source-1"), "supermarkets": {}},
            {**_published_offer(offer_id="two", supermarket_id="sup-2", source_offer_id="source-1"), "supermarkets": {}},
        ],
        count=2,
    )

    response = await _get("/offers")

    assert response.status_code == 428


def test_list_public_offers_does_not_expose_a_sort_query_parameter():
    parameters = test_app.openapi()["paths"]["/offers"]["get"]["parameters"]

    assert "sort" not in {parameter["name"] for parameter in parameters}


@pytest.mark.asyncio
async def test_list_public_offers_filters_by_subcategory():
    sb = MagicMock()
    query = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    query.in_.return_value.eq.return_value.order.return_value.execute.return_value = (
        MagicMock(data=[])
    )

    with (
        patch("api.routers.offers.get_supabase", return_value=sb),
        patch("api.routers.offers.read_guest_location", return_value=("080061", 10)),
        patch(
            "api.routers.offers.nearby_supermarket_distances",
            return_value={"sup-1": 1.2},
        ),
    ):
        response = await _get("/offers?subcategory=Acqua%20e%20Bibite")

    assert response.status_code == 200
    query.eq.assert_called_once_with(
        "subcategory", "Acqua e Bibite"
    )


def _published_offer(
    *,
    offer_id: str,
    supermarket_id: str,
    source_offer_id: str | None,
    name: str = "Snack salmone",
) -> dict:
    return {
        "id": offer_id,
        "name": name,
        "supermarket_id": supermarket_id,
        "source_offer_id": source_offer_id,
    }


def test_deduplicate_nearby_offers_keeps_nearest_selected_target():
    offers = [
        _published_offer(
            offer_id="taurianova", supermarket_id="sup-taurianova", source_offer_id="source-1"
        ),
        _published_offer(
            offer_id="polistena", supermarket_id="sup-polistena", source_offer_id="source-1"
        ),
    ]

    deduplicated = _offers_module._deduplicate_nearby_offers(
        offers,
        {"sup-taurianova": 18.2, "sup-polistena": 1.1},
    )

    assert [offer["id"] for offer in deduplicated] == ["polistena"]


def test_deduplicate_nearby_offers_keeps_selected_store_when_only_target_present():
    offers = [
        _published_offer(
            offer_id="taurianova", supermarket_id="sup-taurianova", source_offer_id="source-1"
        )
    ]

    deduplicated = _offers_module._deduplicate_nearby_offers(
        offers, {"sup-taurianova": 18.2}
    )

    assert [offer["id"] for offer in deduplicated] == ["taurianova"]


@pytest.mark.asyncio
async def test_offer_discovery_reuses_one_nearby_lookup_for_page_and_stores():
    sb = MagicMock()
    request = MagicMock(cookies={})
    page = {"items": [], "total": 0, "nextPage": None}

    with (
        patch("api.routers.offers.get_supabase", return_value=sb),
        patch(
            "api.routers.offers._request_distances",
            return_value={"sup-1": 1.2},
        ) as request_distances,
        patch("api.routers.offers._public_offers_response", return_value=page),
        patch(
            "api.routers.offers.active_nearby_supermarkets",
            return_value=[{"id": "sup-1", "distance_km": 1.2}],
        ) as active_stores,
    ):
        result = await _offers_module.discover_public_offers(
            request=request,
            user_id="user-1",
        )

    assert result == {**page, "supermarkets": [{"id": "sup-1", "distance_km": 1.2}]}
    request_distances.assert_called_once_with(sb, request, "user-1")
    active_stores.assert_called_once_with(sb, {"sup-1": 1.2})


def test_deduplicate_nearby_offers_keeps_independent_offers_separate():
    offers = [
        _published_offer(
            offer_id="one", supermarket_id="sup-1", source_offer_id=None
        ),
        _published_offer(
            offer_id="two", supermarket_id="sup-2", source_offer_id=None
        ),
    ]

    deduplicated = _offers_module._deduplicate_nearby_offers(
        offers, {"sup-1": 1, "sup-2": 2}
    )

    assert [offer["id"] for offer in deduplicated] == ["one", "two"]


def test_deduplicate_nearby_offers_uses_stable_tie_breaking():
    offers = [
        _published_offer(
            offer_id="second", supermarket_id="sup-b", source_offer_id="source-1"
        ),
        _published_offer(
            offer_id="first", supermarket_id="sup-a", source_offer_id="source-1"
        ),
    ]

    deduplicated = _offers_module._deduplicate_nearby_offers(
        offers, {"sup-a": 1, "sup-b": 1}
    )

    assert [offer["id"] for offer in deduplicated] == ["first"]


def test_offer_summary_counts_deduplicated_representatives():
    summary = _offers_module._offer_summary(
        [
            {"supermarket_id": "sup-1", "supermarket_slug": "conad"},
            {"supermarket_id": "sup-1", "supermarket_slug": "conad"},
            {"supermarket_id": "sup-2", "supermarket_slug": "coop"},
        ]
    )

    assert summary == {
        "total": 3,
        "supermarket_count": 2,
        "counts_by_supermarket_id": {"sup-1": 2, "sup-2": 1},
        "counts_by_supermarket_slug": {"conad": 2, "coop": 1},
    }


def test_supermarket_municipality_includes_province_code():
    assert _offers_module._supermarket_municipality(
        {"municipalities": {"name": "Taurianova", "province_code": "RC"}}
    ) == "Taurianova (RC)"


def test_supermarket_municipality_falls_back_to_name():
    assert _offers_module._supermarket_municipality(
        {"municipalities": {"name": "Milano", "province_code": None}}
    ) == "Milano"

class TestCreateManualOffer:
    @pytest.mark.asyncio
    async def test_create_manual_offer_as_admin(self):
        sb = _make_sb()
        with patch("api.routers._offer_utils.get_supabase", return_value=sb), \
             patch("api.routers.offers.get_supabase", return_value=sb):
            resp = await _post(
                "/offers",
                {_DEP_PROFILE: lambda: ADMIN_PROFILE},
                json=VALID_PAYLOAD,
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Mozzarella"
        assert data["is_confirmed"] is False

    @pytest.mark.asyncio
    async def test_create_manual_offer_as_manager_own_supermarket(self):
        sb = _make_sb()
        with patch("api.routers._offer_utils.get_supabase", return_value=sb), \
             patch("api.routers.offers.get_supabase", return_value=sb):
            resp = await _post(
                "/offers",
                {_DEP_PROFILE: lambda: MANAGER_PROFILE},
                json=VALID_PAYLOAD,
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_create_manual_offer_as_manager_foreign_supermarket(self):
        sb = _make_sb()
        with patch("api.routers._offer_utils.get_supabase", return_value=sb), \
             patch("api.routers.offers.get_supabase", return_value=sb):
            resp = await _post(
                "/offers",
                {_DEP_PROFILE: lambda: MANAGER_OTHER_PROFILE},
                json=VALID_PAYLOAD,
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_create_manual_offer_supermarket_not_found(self):
        sb = _make_sb(supermarket_data=None)
        # Override: maybe_single returns no data
        sm_result = MagicMock()
        sm_result.data = None
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = sm_result
        with patch("api.routers._offer_utils.get_supabase", return_value=sb), \
             patch("api.routers.offers.get_supabase", return_value=sb):
            resp = await _post(
                "/offers",
                {_DEP_PROFILE: lambda: ADMIN_PROFILE},
                json=VALID_PAYLOAD,
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_manual_offer_invalid_price(self):
        sb = _make_sb()
        with patch("api.routers._offer_utils.get_supabase", return_value=sb), \
             patch("api.routers.offers.get_supabase", return_value=sb):
            resp = await _post(
                "/offers",
                {_DEP_PROFILE: lambda: ADMIN_PROFILE},
                json={**VALID_PAYLOAD, "price_offer": -1.0},
            )
        assert resp.status_code == 422
