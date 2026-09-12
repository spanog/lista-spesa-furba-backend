"""Comune-only discovery uses ISTAT municipal centers without branch coordinates."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from api.routers._nearby_supermarkets import (
    _with_distances,
    nearby_supermarket_distances,
)
from api.routers.municipalities import _rank_suggestions
from core.guest_location import (
    GUEST_LOCATION_RADIUS_KM,
    GUEST_LOCATION_TTL_SECONDS,
    create_guest_location_token,
)


def test_polistena_area_keeps_selected_municipality_before_nearby_branches():
    rows = [
        {"id": "polistena-far", "municipality_code": "080061"},
        {"id": "cinquefrondi-near", "municipality_code": "080027"},
    ]

    visible = _with_distances(
        rows,
        {"cinquefrondi-near": 3.4, "polistena-far": 24.2},
        "080061",
    )

    assert [row["id"] for row in visible] == [
        "polistena-far",
        "cinquefrondi-near",
    ]


def test_milano_area_keeps_all_milan_branches_and_nearby_external_branches():
    rows = [
        {"id": "milan-far", "municipality_code": "015146"},
        {"id": "nearby-external", "municipality_code": "015209"},
    ]

    visible = _with_distances(
        rows,
        {"nearby-external": 8.1, "milan-far": 18.5},
        "015146",
    )

    assert [row["id"] for row in visible] == ["milan-far", "nearby-external"]


def test_radius_rpc_receives_istat_code_not_user_coordinates():
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value.data = [
        {"id": "inside", "distance_km": 2.5},
        {"id": "selected-without-coordinates", "distance_km": None},
    ]

    distances = nearby_supermarket_distances(sb, "080061", 10)

    assert distances == {"inside": 2.5, "selected-without-coordinates": None}
    sb.rpc.assert_called_once_with(
        "visible_supermarkets_for_municipality",
        {"selected_municipality_code": "080061", "radius_m": 10_000},
    )


def test_municipality_search_prioritizes_the_exact_comune_name():
    ranked = _rank_suggestions(
        [
            {"name": "Arcinazzo Romano", "normalized_name": "arcinazzo romano"},
            {"name": "Roma", "normalized_name": "roma"},
            {"name": "Roma", "normalized_name": "roma"},
            {"name": "Romano di Lombardia", "normalized_name": "romano di lombardia"},
        ],
        "roma",
    )

    assert [row["name"] for row in ranked] == [
        "Roma",
        "Roma",
        "Romano di Lombardia",
        "Arcinazzo Romano",
    ]


def test_guest_cookie_claims_contain_only_the_municipality_code_and_radius():
    with patch("core.guest_location.create_session_token", return_value="token") as create:
        create_guest_location_token("080061")

    create.assert_called_once_with(
        {
            "typ": "guest_location",
            "municipality_code": "080061",
            "radius": GUEST_LOCATION_RADIUS_KM,
        },
        lifetime_seconds=GUEST_LOCATION_TTL_SECONDS,
    )
