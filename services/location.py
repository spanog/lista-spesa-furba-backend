from __future__ import annotations


def nearby_distances(
    sb, municipality_code: str, max_distance_km: float
) -> dict[str, float | None]:
    response = sb.rpc(
        "visible_supermarkets_for_municipality",
        {
            "selected_municipality_code": municipality_code,
            "radius_m": max_distance_km * 1000,
        },
    ).execute()
    return {row["id"]: row["distance_km"] for row in (response.data or [])}


def load_nearby_distances(sb, user_id: str) -> dict[str, float | None] | None:
    """Return {supermarket_id: distance_km} for user's search radius, or None."""
    profile_resp = (
        sb.table("user_profiles")
        .select("municipality_code, max_distance_km")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    profile: dict = (profile_resp.data if profile_resp is not None else None) or {}
    municipality_code = profile.get("municipality_code")
    max_km = profile.get("max_distance_km") or 10
    if municipality_code is None:
        return None
    return nearby_distances(sb, str(municipality_code), max_km)
