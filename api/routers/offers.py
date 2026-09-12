"""Manual offer creation — not tied to a flyer."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from core.auth import get_optional_user_id, managed_supermarket_ids, require_admin_or_manager
from core.database import get_supabase
from core.guest_location import GUEST_LOCATION_COOKIE, guest_location_required, read_guest_location
from api.routers._nearby_supermarkets import (
    active_nearby_supermarkets,
    nearby_supermarket_distances,
    request_location,
)
from services.extraction.normalizer import normalize_unit_price_measure
from services.offer_visibility import apply_current_offer_window
from services.product_format import ProductFormat
from api.routers._offer_utils import build_offer_row, insert_and_fetch_offer

router = APIRouter()

PUBLIC_OFFER_SELECT = "*, supermarkets(name, slug, logo_url, municipality_code, municipalities(name,province_code))"


def _offer_group_key(offer: dict) -> str:
    """Keep independently published offers separate from cloned flyer offers."""
    source_offer_id = offer.get("source_offer_id")
    return f"source:{source_offer_id}" if source_offer_id else f"offer:{offer['id']}"


def _deduplicate_nearby_offers(
    offers: list[dict], distances_by_supermarket_id: dict[str, float | None]
) -> list[dict]:
    """Choose nearest target for each cloned source offer with deterministic ties."""
    representatives: dict[str, dict] = {}
    for offer in offers:
        enriched = {
            **offer,
            "distance_km": distances_by_supermarket_id.get(offer["supermarket_id"]),
        }
        group_key = _offer_group_key(enriched)
        current = representatives.get(group_key)
        if current is None or _offer_distance_sort_key(enriched) < _offer_distance_sort_key(current):
            representatives[group_key] = enriched
    return sorted(
        representatives.values(),
        key=lambda offer: ((offer.get("name") or "").casefold(), offer["id"]),
    )


def _offer_distance_sort_key(offer: dict) -> tuple[float, str, str]:
    return (
        float(offer.get("distance_km") or float("inf")),
        offer.get("supermarket_id") or "",
        offer["id"],
    )


def _offer_summary(offers: list[dict]) -> dict:
    counts_by_supermarket_id: dict[str, int] = {}
    counts_by_supermarket_slug: dict[str, int] = {}
    for offer in offers:
        supermarket_id = offer.get("supermarket_id")
        if supermarket_id:
            counts_by_supermarket_id[supermarket_id] = (
                counts_by_supermarket_id.get(supermarket_id, 0) + 1
            )
        supermarket_slug = offer.get("supermarket_slug")
        if supermarket_slug:
            counts_by_supermarket_slug[supermarket_slug] = (
                counts_by_supermarket_slug.get(supermarket_slug, 0) + 1
            )
    return {
        "total": len(offers),
        "supermarket_count": len(counts_by_supermarket_id),
        "counts_by_supermarket_id": counts_by_supermarket_id,
        "counts_by_supermarket_slug": counts_by_supermarket_slug,
    }


def _supermarket_municipality(supermarket: dict) -> str | None:
    municipality = supermarket.get("municipalities") or {}
    name = municipality.get("name")
    province = municipality.get("province_code")
    if not name:
        return None
    return f"{name} ({province})" if province else name


def _public_offers_query(sb, *, exact_count: bool):
    offers = sb.table("offers")
    query = (
        offers.select(PUBLIC_OFFER_SELECT, count="exact")
        if exact_count
        else offers.select(PUBLIC_OFFER_SELECT)
    )
    query = query.eq("is_confirmed", True).eq("offer_kind", "published_target")
    return apply_current_offer_window(query)


def _filter_public_offers(
    query,
    *,
    q: str | None,
    category: str | None,
    subcategory: str | None,
    supermarket_id: str | None,
    supermarket_ids: list[str],
):
    if q:
        query = query.ilike("name", f"%{q.strip()}%")
    if category:
        query = query.eq("category", category)
    if subcategory:
        query = query.eq("subcategory", subcategory)
    if supermarket_id:
        query = query.eq("supermarket_id", supermarket_id)
    if supermarket_ids:
        query = query.in_("supermarket_id", list(dict.fromkeys(supermarket_ids)))
    return query


def _public_offers_response(
    sb,
    *,
    q: str | None,
    category: str | None,
    subcategory: str | None,
    supermarket_id: str | None,
    supermarket_ids: list[str],
    limit: int,
    offset: int,
    distances_by_supermarket_id: dict[str, float | None] | None,
) -> dict:
    query = _filter_public_offers(
        _public_offers_query(sb, exact_count=distances_by_supermarket_id is None),
        q=q,
        category=category,
        subcategory=subcategory,
        supermarket_id=supermarket_id,
        supermarket_ids=supermarket_ids,
    )
    if distances_by_supermarket_id is not None:
        query = query.in_("supermarket_id", list(distances_by_supermarket_id))
    ordered_query = query.order("name")
    if distances_by_supermarket_id is None:
        response = ordered_query.range(offset, offset + limit - 1).execute()
        items = _serialize_offers(response.data or [])
        total = response.count or 0
        return _offer_page(items, total, offset, limit)
    offers = _serialize_offers(ordered_query.execute().data or [])
    offers = _deduplicate_nearby_offers(offers, distances_by_supermarket_id)
    return _nearby_offer_page(offers, offset, limit)


def _offer_page(items: list[dict], total: int, offset: int, limit: int) -> dict:
    return {
        "items": items,
        "total": total,
        "nextPage": offset + limit if offset + limit < total else None,
    }


def _nearby_offer_page(offers: list[dict], offset: int, limit: int) -> dict:
    summary = _offer_summary(offers)
    return {
        "items": offers[offset : offset + limit],
        **summary,
        "nextPage": offset + limit if offset + limit < summary["total"] else None,
    }


def _request_distances(
    sb, request: Request, user_id: str | None
) -> dict[str, float | None] | None:
    guest_token = request.cookies.get(GUEST_LOCATION_COOKIE) if user_id is None else None
    guest_location = read_guest_location(guest_token)
    if user_id is None and guest_location is None:
        raise guest_location_required(clear_cookie=guest_token is not None)
    location = request_location(sb, user_id, guest_location)
    if location is None:
        return None
    return nearby_supermarket_distances(
        sb, location.municipality_code, location.max_distance_km
    )


@router.get("")
async def list_public_offers(
    q: str | None = Query(None),
    category: str | None = Query(None),
    subcategory: str | None = Query(None),
    supermarket_id: str | None = Query(None),
    supermarket_ids: list[str] = Query(default=[]),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    request: Request = None,
    user_id: str | None = Depends(get_optional_user_id),
) -> dict:
    """Return currently visible offers; offer fields are self-contained."""
    sb = get_supabase()
    distances = _request_distances(sb, request, user_id)
    if distances == {}:
        return {"items": [], "total": 0, "nextPage": None}
    return _public_offers_response(
        sb, q=q, category=category, subcategory=subcategory,
        supermarket_id=supermarket_id, supermarket_ids=supermarket_ids,
        limit=limit, offset=offset, distances_by_supermarket_id=distances,
    )


@router.get("/discovery")
async def discover_public_offers(
    q: str | None = Query(None),
    category: str | None = Query(None),
    subcategory: str | None = Query(None),
    supermarket_id: str | None = Query(None),
    supermarket_ids: list[str] = Query(default=[]),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    request: Request = None,
    user_id: str | None = Depends(get_optional_user_id),
) -> dict:
    """Return first offer page and nearby active branches from one radius lookup."""
    sb = get_supabase()
    distances = _request_distances(sb, request, user_id)
    if distances == {}:
        return {"items": [], "total": 0, "nextPage": None, "supermarkets": []}
    page = _public_offers_response(
        sb, q=q, category=category, subcategory=subcategory,
        supermarket_id=supermarket_id, supermarket_ids=supermarket_ids,
        limit=limit, offset=offset, distances_by_supermarket_id=distances,
    )
    return {
        **page,
        "supermarkets": active_nearby_supermarkets(sb, distances or {}),
    }


def _serialize_offers(rows: list[dict]) -> list[dict]:
    offers = []
    for row in rows:
        supermarket = row.pop("supermarkets", None) or {}
        offers.append({
            **row,
            "supermarket_name": supermarket.get("name") or row.get("supermarket_name"),
            "supermarket_slug": supermarket.get("slug"),
            "supermarket_logo_url": supermarket.get("logo_url"),
            "supermarket_municipality": _supermarket_municipality(supermarket),
        })
    return offers


class ManualOfferCreate(BaseModel):
    supermarket_id: str
    name: str = Field(..., min_length=1)
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    format: ProductFormat = Field(default_factory=ProductFormat)
    price_offer: float = Field(..., gt=0)
    price_original: float | None = Field(None, gt=0)
    unit_price_value: float | None = Field(None, gt=0)
    unit_price_unit: str | None = None
    offer_notes: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_manual_offer(
    payload: ManualOfferCreate,
    profile: Annotated[dict, Depends(require_admin_or_manager)],
) -> dict:
    if profile.get("role") == "supermarket_manager":
        if payload.supermarket_id not in managed_supermarket_ids(profile):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Managers can only create offers for their own supermarket")
    sb = get_supabase()
    sm = sb.table("supermarkets").select("id, name").eq("id", payload.supermarket_id).maybe_single().execute()
    if not sm or not sm.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supermarket not found")
    normalized_unit = normalize_unit_price_measure(payload.unit_price_unit) if payload.unit_price_unit else None
    offer_row = build_offer_row(payload, sm.data["id"], sm.data["name"], None, normalized_unit)
    return insert_and_fetch_offer(sb, offer_row)
