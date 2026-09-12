"""Shared geographic lookup helpers for public discovery endpoints."""
from __future__ import annotations

from dataclasses import dataclass

_CURRENT_PUBLIC_OFFER_SUPERMARKETS_RPC = "current_public_offer_supermarket_ids"
PUBLIC_DISCOVERY_SUPERMARKET_SELECT = (
    "id,name,slug,logo_url,color_hex,website_url,is_active,created_at,municipality_code,"
    "municipalities(name,province_code)"
)


@dataclass(frozen=True)
class DiscoveryArea:
    municipality_code: str
    max_distance_km: float


def nearby_supermarket_distances(
    sb, municipality_code: str, max_distance_km: float
) -> dict[str, float | None]:
    """Map visible branches to their distance from the selected Comune center."""
    response = sb.rpc(
        "visible_supermarkets_for_municipality",
        {
            "selected_municipality_code": municipality_code,
            "radius_m": max_distance_km * 1000,
        },
    ).execute()
    return {
        row["id"]: _distance_value(row.get("distance_km"))
        for row in (response.data or [])
        if row.get("id") is not None
    }


def _distance_value(value: object) -> float | None:
    return float(value) if value is not None else None


def active_nearby_supermarkets(
    sb,
    distances_by_supermarket_id: dict[str, float | None],
    municipality_code: str | None = None,
) -> list[dict]:
    """Return nearby branches with at least one public, current offer."""
    ids = list(distances_by_supermarket_id)
    if not ids:
        return []
    offer_ids = _active_offer_supermarket_ids(sb, ids)
    if not offer_ids:
        return []
    rows = (
        sb.table("supermarkets")
        .select(PUBLIC_DISCOVERY_SUPERMARKET_SELECT)
        .in_("id", offer_ids)
        .execute()
        .data
        or []
    )
    return _with_distances(rows, distances_by_supermarket_id, municipality_code)


def public_supermarket(row: dict) -> dict:
    """Expose branch identity and Comune only; never address or coordinates."""
    municipality = row.get("municipalities") or {}
    return {
        key: value
        for key, value in {
            **row,
            "municipality_name": municipality.get("name"),
            "municipality_province_code": municipality.get("province_code"),
        }.items()
        if key not in {"address", "city", "province", "postal_code", "lat", "lng", "location", "municipalities"}
    }


def _active_offer_supermarket_ids(sb, supermarket_ids: list[str]) -> list[str]:
    response = sb.rpc(
        _CURRENT_PUBLIC_OFFER_SUPERMARKETS_RPC,
        {"candidate_supermarket_ids": supermarket_ids},
    )
    rows = response.execute().data or []
    return [row["id"] for row in rows if row.get("id")]


def _with_distances(
    rows: list[dict], distances: dict[str, float | None], municipality_code: str | None
) -> list[dict]:
    enriched = [
        {**public_supermarket(row), "distance_km": distances[row["id"]]}
        for row in rows
        if row.get("id") in distances
    ]
    positions = {supermarket_id: index for index, supermarket_id in enumerate(distances)}
    return sorted(
        enriched,
        key=lambda row: _distance_sort_key(row, positions, municipality_code),
    )


def _distance_sort_key(
    row: dict, positions: dict[str, int], municipality_code: str | None
) -> tuple:
    selected_first = row.get("municipality_code") != municipality_code
    ordered_position = positions[row["id"]]
    return (
        selected_first if municipality_code else False,
        ordered_position,
    )


def request_location(
    sb, user_id: str | None, guest_location: tuple[str, float] | None
) -> DiscoveryArea | None:
    """Resolve the Comune-only discovery area for members and guests."""
    if user_id is None:
        return _guest_area(guest_location)
    profile = _location_profile(sb, user_id)
    municipality_code = profile.get("municipality_code")
    if not municipality_code:
        return None
    radius = profile.get("max_distance_km") or 10.0
    return DiscoveryArea(str(municipality_code), float(radius))


def _guest_area(location: tuple[str, float] | None) -> DiscoveryArea | None:
    if location is None:
        return None
    municipality_code, radius = location
    return DiscoveryArea(municipality_code, radius)


def _location_profile(sb, user_id: str) -> dict:
    return (
        sb.table("user_profiles")
        .select("municipality_code, max_distance_km")
        .eq("id", user_id)
        .maybe_single()
        .execute()
        .data
        or {}
    )
