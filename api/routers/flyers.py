from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import uuid
import hashlib
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from pydantic import BaseModel, Field

from core.auth import (
    assert_flyer_access,
    get_current_user_id,
    get_optional_user_id,
    managed_supermarket_ids,
    require_admin_or_manager,
)
from api.routers._nearby_supermarkets import (
    PUBLIC_DISCOVERY_SUPERMARKET_SELECT,
    nearby_supermarket_distances,
    public_supermarket,
    request_location,
)
from core.config import settings
from core.database import get_supabase
from core.guest_location import GUEST_LOCATION_COOKIE, guest_location_required, read_guest_location
from services.extraction.normalizer import format_unit_price_label, normalize_unit_price_measure
from services.notification_jobs import enqueue_flyer_published, reschedule_flyer_published
from services.product_format import ProductFormat, build_format_bundle
from services.flyer_preview import render_flyer_preview
from services.offer_visibility import apply_current_offer_window
from api.routers._offer_utils import (
    _OFFER_PRODUCT_SELECT,
    _flatten_draft_offer,
    build_format_fields,
    build_offer_row,
    draft_product_key,
    insert_and_fetch_offer,
)

router = APIRouter()

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_PRODUCT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_PRODUCT_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
OFFER_KIND_SOURCE_MASTER = "source_master"
OFFER_KIND_PUBLISHED_TARGET = "published_target"
PROCESSING_RESUME_STALE_AFTER = timedelta(minutes=5)
MAX_PUBLIC_FLYERS = 1_000


class DraftOfferUpdate(BaseModel):
    name: str | None = None
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    format: ProductFormat | None = None
    price_offer: float | None = Field(None, gt=0)
    price_original: float | None = Field(None, gt=0)
    unit_price_value: float | None = Field(None, gt=0)
    unit_price_unit: str | None = None
    offer_notes: str | None = None
    is_reviewed: bool | None = None


class DraftOfferCreate(BaseModel):
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


class FlyerValidityUpdate(BaseModel):
    valid_from: str | None = None
    valid_to: str | None = None


class FlyerTargetsUpdate(BaseModel):
    supermarket_ids: list[str] = Field(default_factory=list, min_length=1)


class FlyerSignedUploadRequest(BaseModel):
    file_name: str = Field(..., min_length=1)
    content_type: str = Field(..., min_length=1)
    size_bytes: int = Field(..., gt=0, le=MAX_FILE_SIZE)
    supermarket_ids: list[str] = Field(default_factory=list)


class FlyerSignedUploadResponse(BaseModel):
    bucket: str
    path: str
    token: str
    signed_url: str


class FlyerUploadCompleteRequest(BaseModel):
    storage_path: str = Field(..., min_length=1)
    file_name: str = Field(..., min_length=1)
    content_type: str = Field(..., min_length=1)
    supermarket_ids: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _can_resume_stale_processing(flyer: dict) -> bool:
    if flyer.get("status") != "processing":
        return False
    metadata = flyer.get("extraction_metadata")
    if not isinstance(metadata, dict):
        return False
    if not metadata.get("next_chunk_index") or not metadata.get("last_completed_chunk"):
        return False
    updated_at = _parse_timestamp(flyer.get("updated_at"))
    if updated_at is None:
        return False
    return datetime.now(timezone.utc) - updated_at >= PROCESSING_RESUME_STALE_AFTER


def _confirmed_count_by_flyer(sb, flyer_ids: list[str]) -> dict[str, int]:
    return _offer_count_by_flyer(sb, flyer_ids, is_confirmed=True)


def _is_flyer_current(flyer: dict, today: date) -> bool:
    valid_from = flyer.get("valid_from")
    valid_to = flyer.get("valid_to")
    if valid_from and date.fromisoformat(str(valid_from)) > today:
        return False
    if valid_to and date.fromisoformat(str(valid_to)) < today:
        return False
    return True


def _public_flyer_expiry_sort_key(flyer: dict) -> date:
    """Keep flyers without an expiry date after dated flyers."""
    valid_to = flyer.get("valid_to")
    return date.fromisoformat(str(valid_to)) if valid_to else date.max


def _public_flyers(sb, supermarket_ids: list[str]) -> list[dict]:
    if not supermarket_ids:
        return []
    query = (
        sb.table("flyers")
        .select("*")
        .eq("flyer_kind", OFFER_KIND_PUBLISHED_TARGET)
        .eq("status", "done")
        .eq("is_public", True)
        .in_("supermarket_id", supermarket_ids)
        .order("created_at", desc=True)
        .limit(MAX_PUBLIC_FLYERS)
    )
    return apply_current_offer_window(query).execute().data or []


def _public_flyer_context(
    sb, request: Request, user_id: str | None
) -> tuple[list[dict], dict[str, float | None]]:
    guest_token = request.cookies.get(GUEST_LOCATION_COOKIE) if user_id is None else None
    guest_location = read_guest_location(guest_token)
    if user_id is None and guest_location is None:
        raise guest_location_required(clear_cookie=guest_token is not None)
    location = request_location(sb, user_id, guest_location)
    if location is None:
        return [], {}
    distances = nearby_supermarket_distances(
        sb, location.municipality_code, location.max_distance_km
    )
    flyers = _public_flyers(sb, list(distances))
    return flyers, distances


def _nearby_supermarket_rows(sb, distances: dict[str, float | None]) -> list[dict]:
    if not distances:
        return []
    rows = (
        sb.table("supermarkets")
        .select(PUBLIC_DISCOVERY_SUPERMARKET_SELECT)
        .in_("id", list(distances))
        .execute()
        .data
        or []
    )
    visible = [
        {**row, "distance_km": distances[row["id"]]}
        for row in rows
        if row.get("id") in distances
    ]
    positions = {supermarket_id: index for index, supermarket_id in enumerate(distances)}
    return sorted(visible, key=lambda row: positions[row["id"]])


def _visible_public_flyers(
    sb, flyers: list[dict], distances: dict[str, float | None]
) -> list[dict]:
    if not flyers:
        return []
    confirmed_by_flyer = _confirmed_count_by_flyer(sb, [flyer["id"] for flyer in flyers])
    today = datetime.now(timezone.utc).date()
    visible: list[dict] = []
    for flyer in flyers:
        if not _is_flyer_current(flyer, today):
            continue
        confirmed_count = confirmed_by_flyer.get(flyer["id"], 0)
        if confirmed_count > 0:
            flyer["confirmed_count"] = confirmed_count
            visible.append(_public_flyer_representation(flyer))
    return sorted(visible, key=lambda flyer: _public_flyer_sort_key(flyer, distances))


def _public_flyer_sort_key(flyer: dict, distances: dict[str, float | None]) -> tuple:
    positions = {supermarket_id: index for index, supermarket_id in enumerate(distances)}
    return (
        positions[flyer["supermarket_id"]],
        _public_flyer_expiry_sort_key(flyer),
        flyer["id"],
    )


def _offer_count_by_flyer(
    sb,
    flyer_ids: list[str],
    *,
    is_confirmed: bool,
) -> dict[str, int]:
    if not flyer_ids:
        return {}

    result = sb.rpc(
        "count_offers_by_flyer",
        {
            "p_flyer_ids": flyer_ids,
            "p_is_confirmed": is_confirmed,
        },
    ).execute()
    return {
        row["flyer_id"]: int(row["offer_count"])
        for row in result.data or []
    }


def _published_target_count_by_source_flyer(
    sb,
    source_flyer_ids: list[str],
) -> dict[str, int]:
    if not source_flyer_ids:
        return {}

    result = (
        sb.table("flyers")
        .select("source_flyer_id")
        .eq("flyer_kind", "published_target")
        .in_("source_flyer_id", source_flyer_ids)
        .execute()
    )
    counts: dict[str, int] = {}
    for row in result.data or []:
        source_flyer_id = row.get("source_flyer_id")
        if not source_flyer_id:
            continue
        counts[source_flyer_id] = counts.get(source_flyer_id, 0) + 1
    return counts


def _has_confirmed_offers(sb, flyer_id: str) -> bool:
    result = (
        sb.table("offers")
        .select("id", count="exact")
        .eq("flyer_id", flyer_id)
        .eq("is_confirmed", True)
        .execute()
    )
    return (result.count or 0) > 0


def _offer_kind(offer: dict) -> str:
    return offer.get("offer_kind") or OFFER_KIND_SOURCE_MASTER


def _flyer_targets(sb, flyer_id: str) -> list[dict]:
    result = (
        sb.table("flyer_targets")
        .select("id, supermarket_id, supermarkets(id, name, municipality_code, logo_url, municipalities(name,province_code))")
        .eq("flyer_id", flyer_id)
        .execute()
    )
    targets: list[dict] = []
    for row in result.data or []:
        supermarket = row.get("supermarkets") or {}
        targets.append(
            {
                "id": row.get("id"),
                "supermarket_id": row["supermarket_id"],
                "supermarket_name": supermarket.get("name"),
                "municipality_code": supermarket.get("municipality_code"),
                "municipality_name": (supermarket.get("municipalities") or {}).get("name"),
                "municipality_province_code": (supermarket.get("municipalities") or {}).get("province_code"),
                "logo_url": supermarket.get("logo_url"),
            }
        )
    return targets


def _enrich_flyer(
    sb,
    flyer: dict,
    *,
    source_draft_count: int | None = None,
    source_confirmed_count: int | None = None,
    published_target_count: int | None = None,
) -> dict:
    enriched = dict(flyer)
    enriched["flyer_kind"] = flyer.get("flyer_kind") or "source"
    enriched["targets"] = _flyer_targets(sb, flyer["id"]) if enriched["flyer_kind"] == "source" else []
    if source_draft_count is not None:
        enriched["draft_count"] = source_draft_count
    if source_confirmed_count is not None:
        enriched["confirmed_count"] = source_confirmed_count
    if published_target_count is not None:
        enriched["published_target_count"] = published_target_count
    return enriched


def _source_flyer_required(flyer: dict) -> None:
    if (flyer.get("flyer_kind") or "source") != "source":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This action is only available on source flyers",
        )


def _published_target_flyers(sb, source_flyer_id: str) -> dict[str, dict]:
    result = (
        sb.table("flyers")
        .select("id, supermarket_id, supermarket_name")
        .eq("source_flyer_id", source_flyer_id)
        .eq("flyer_kind", "published_target")
        .execute()
    )
    return {
        row["supermarket_id"]: {
            "flyer_id": row["id"],
            "supermarket_name": row.get("supermarket_name") or "Supermercato",
        }
        for row in (result.data or [])
        if row.get("id") and row.get("supermarket_id")
    }


def _source_master_offers(sb, flyer_id: str) -> list[dict]:
    result = (
        sb.table("offers")
        .select("*")
        .eq("flyer_id", flyer_id)
        .eq("is_confirmed", True)
        .eq("offer_kind", OFFER_KIND_SOURCE_MASTER)
        .execute()
    )
    return result.data or []


def _delete_removed_published_targets(
    sb,
    *,
    target_flyers: dict[str, dict],
    desired_supermarket_ids: set[str],
) -> None:
    stale_flyer_ids = [
        target["flyer_id"]
        for supermarket_id, target in target_flyers.items()
        if supermarket_id not in desired_supermarket_ids
    ]
    if not stale_flyer_ids:
        return
    sb.table("offers").delete().in_("flyer_id", stale_flyer_ids).eq(
        "offer_kind",
        OFFER_KIND_PUBLISHED_TARGET,
    ).execute()
    sb.table("flyers").delete().in_("id", stale_flyer_ids).execute()


def _clone_offer_fields(
    source_offer: dict,
    *,
    flyer_id: str,
    supermarket_id: str,
    supermarket_name: str,
) -> dict:
    return {
        "name": source_offer.get("name"),
        "brand": source_offer.get("brand"),
        "category": source_offer.get("category"),
        "subcategory": source_offer.get("subcategory"),
        "offer_key": source_offer.get("offer_key"),
        "image_url": source_offer.get("image_url"),
        "packshot_source_page": source_offer.get("packshot_source_page"),
        "packshot_bbox": source_offer.get("packshot_bbox"),
        "flyer_id": flyer_id,
        "supermarket_id": supermarket_id,
        "supermarket_name": supermarket_name,
        "price_original": source_offer.get("price_original"),
        "price_offer": source_offer.get("price_offer"),
        "discount_pct": source_offer.get("discount_pct"),
        "unit_price": source_offer.get("unit_price"),
        "unit_price_value": source_offer.get("unit_price_value"),
        "unit_price_unit": source_offer.get("unit_price_unit"),
        "offer_type": source_offer.get("offer_type"),
        "offer_notes": source_offer.get("offer_notes"),
        "valid_from": source_offer.get("valid_from"),
        "valid_to": source_offer.get("valid_to"),
        "raw_text": source_offer.get("raw_text"),
        "confidence_score": source_offer.get("confidence_score"),
        "format": source_offer.get("format"),
        "format_key": source_offer.get("format_key"),
        "format_label": source_offer.get("format_label"),
        "is_confirmed": True,
        "is_reviewed": source_offer.get("is_reviewed", False),
        "offer_kind": OFFER_KIND_PUBLISHED_TARGET,
        "source_offer_id": source_offer["id"],
    }


def _sync_published_clones_for_source_offers(
    sb,
    *,
    source_offers: list[dict],
    target_flyers: dict[str, dict],
) -> dict[str, int]:
    if not target_flyers:
        return {}

    if not source_offers:
        stale_rows = (
            sb.table("offers")
            .select("id")
            .in_("flyer_id", [target["flyer_id"] for target in target_flyers.values()])
            .eq("offer_kind", OFFER_KIND_PUBLISHED_TARGET)
            .execute()
        ).data or []
        stale_ids = [row["id"] for row in stale_rows if row.get("id")]
        if stale_ids:
            sb.table("offers").delete().in_("id", stale_ids).execute()
        return {target["flyer_id"]: 0 for target in target_flyers.values()}

    source_offer_ids = [source_offer["id"] for source_offer in source_offers]
    target_supermarket_ids = list(target_flyers.keys())
    existing_rows = (
        sb.table("offers")
        .select("id, flyer_id, supermarket_id, source_offer_id")
        .in_("source_offer_id", source_offer_ids)
        .in_("supermarket_id", target_supermarket_ids)
        .eq("offer_kind", OFFER_KIND_PUBLISHED_TARGET)
        .execute()
    ).data or []
    existing_by_key = {
        (row["source_offer_id"], row["supermarket_id"]): row
        for row in existing_rows
        if row.get("id") and row.get("source_offer_id") and row.get("supermarket_id")
    }

    desired_keys: set[tuple[str, str]] = set()
    existing_payloads: list[dict] = []
    missing_payloads: list[dict] = []
    counts_by_flyer: dict[str, int] = {}

    for source_offer in source_offers:
        for supermarket_id, target in target_flyers.items():
            payload = _clone_offer_fields(
                source_offer,
                flyer_id=target["flyer_id"],
                supermarket_id=supermarket_id,
                supermarket_name=target["supermarket_name"],
            )
            desired_keys.add((source_offer["id"], supermarket_id))
            counts_by_flyer[target["flyer_id"]] = counts_by_flyer.get(target["flyer_id"], 0) + 1
            existing = existing_by_key.get((source_offer["id"], supermarket_id))
            if existing:
                existing_payloads.append({"id": existing["id"], **payload})
            else:
                clone = {"id": str(uuid.uuid4()), **payload}
                missing_payloads.append(clone)

    if existing_payloads:
        sb.table("offers").upsert(existing_payloads, on_conflict="id").execute()
    if missing_payloads:
        sb.table("offers").insert(missing_payloads).execute()

    stale_clone_ids = [
        row["id"]
        for row in existing_rows
        if (row.get("source_offer_id"), row.get("supermarket_id")) not in desired_keys
    ]
    if stale_clone_ids:
        sb.table("offers").delete().in_("id", stale_clone_ids).execute()

    return counts_by_flyer


def _sync_published_clones_for_source_offer(
    sb,
    *,
    source_offer: dict,
    target_flyers: dict[str, dict],
) -> None:
    _sync_published_clones_for_source_offers(
        sb,
        source_offers=[source_offer],
        target_flyers=target_flyers,
    )


def _upsert_published_target_flyer(
    sb,
    *,
    source_flyer: dict,
    target: dict,
    existing_flyer_id: str | None,
    products_count: int,
    notify_new: bool,
) -> str:
    target_supermarket_id = target["supermarket_id"]
    target_supermarket_name = target.get("supermarket_name") or "Supermercato"
    fields = {
        "supermarket_name": target_supermarket_name,
        "status": "done",
        "valid_from": source_flyer.get("valid_from"),
        "valid_to": source_flyer.get("valid_to"),
        "is_public": True,
    }
    if existing_flyer_id:
        sb.table("flyers").update(fields).eq("id", existing_flyer_id).execute()
        return existing_flyer_id
    inserted = (
        sb.table("flyers")
        .insert({
            **fields,
            "user_id": source_flyer.get("user_id"),
            "supermarket_id": target_supermarket_id,
            "file_url": source_flyer.get("file_url"),
            "file_type": source_flyer.get("file_type"),
            "file_name": source_flyer.get("file_name"),
            "preview_path": source_flyer.get("preview_path"),
            "products_count": 0,
            "pages_count": source_flyer.get("pages_count"),
            "extraction_metadata": source_flyer.get("extraction_metadata"),
            "file_hash": source_flyer.get("file_hash"),
            "flyer_kind": "published_target",
            "source_flyer_id": source_flyer["id"],
        })
        .execute()
    )
    flyer_id = inserted.data[0]["id"]
    if notify_new:
        enqueue_flyer_published(
            sb,
            flyer_id=flyer_id,
            supermarket_id=target_supermarket_id,
            supermarket_name=target_supermarket_name,
            products_count=products_count,
            valid_from=source_flyer.get("valid_from"),
        )
    return flyer_id


def _sync_published_targets_for_source_flyer(
    sb,
    *,
    source_flyer: dict,
    targets: list[dict],
    notify_new: bool,
    source_offers: list[dict] | None = None,
) -> dict[str, int]:
    if source_offers is None:
        source_offers = _source_master_offers(sb, source_flyer["id"])
    target_flyers = _published_target_flyers(sb, source_flyer["id"])
    desired_ids = {target["supermarket_id"] for target in targets}
    _delete_removed_published_targets(
        sb,
        target_flyers=target_flyers,
        desired_supermarket_ids=desired_ids,
    )
    for target in targets:
        existing = target_flyers.get(target["supermarket_id"])
        _upsert_published_target_flyer(
            sb,
            source_flyer=source_flyer,
            target=target,
            existing_flyer_id=existing["flyer_id"] if existing else None,
            products_count=len(source_offers),
            notify_new=notify_new,
        )
    target_flyers = _published_target_flyers(sb, source_flyer["id"])
    counts = _sync_published_clones_for_source_offers(
        sb,
        source_offers=source_offers,
        target_flyers={
            key: value
            for key, value in target_flyers.items()
            if key in desired_ids
        },
    )
    for published_flyer_id, count in counts.items():
        sb.table("flyers").update({"products_count": count}).eq(
            "id",
            published_flyer_id,
        ).execute()
    return counts


def _profile_supermarket_ids(profile: dict) -> list[str]:
    return managed_supermarket_ids(profile)


def _manager_target_ids(sb, flyer_id: str) -> set[str]:
    return {target["supermarket_id"] for target in _flyer_targets(sb, flyer_id)}


def _supermarket_name_map(sb, supermarket_ids: list[str]) -> dict[str, str]:
    if not supermarket_ids:
        return {}
    result = sb.table("supermarkets").select("id, name").in_("id", supermarket_ids).execute()
    return {
        row["id"]: row.get("name") or "Supermercato"
        for row in (result.data or [])
        if row.get("id")
    }


def _resolve_upload_supermarket_ids(profile: dict, supermarket_ids: list[str]) -> list[str]:
    requested = [value for value in supermarket_ids if value]
    if profile.get("role") != "supermarket_manager":
        return requested

    allowed = _profile_supermarket_ids(profile)
    if not requested:
        if len(allowed) == 1:
            return allowed
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one managed supermarket",
        )
    forbidden = [value for value in requested if value not in allowed]
    if forbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managers can only upload flyers for their assigned supermarkets",
        )
    return requested


def _duplicate_target_conflicts(
    sb,
    *,
    file_hash: str,
    supermarket_ids: list[str],
    exclude_source_flyer_id: str | None = None,
) -> set[str]:
    if not supermarket_ids:
        return set()

    flyers_resp = (
        sb.table("flyers")
        .select("id, flyer_kind, supermarket_id, source_flyer_id")
        .eq("file_hash", file_hash)
        .execute()
    )
    rows = flyers_resp.data or []
    conflicts = {
        row["supermarket_id"]
        for row in rows
        if row.get("flyer_kind") == "published_target"
        and row.get("supermarket_id") in supermarket_ids
        and row.get("source_flyer_id") != exclude_source_flyer_id
    }

    source_ids = [
        row["id"]
        for row in rows
        if row.get("flyer_kind") == "source"
        and row.get("id") != exclude_source_flyer_id
    ]
    if source_ids:
        targets_resp = (
            sb.table("flyer_targets")
            .select("supermarket_id")
            .in_("flyer_id", source_ids)
            .in_("supermarket_id", supermarket_ids)
            .execute()
        )
        conflicts.update(
            row["supermarket_id"]
            for row in (targets_resp.data or [])
            if row.get("supermarket_id")
        )
    return conflicts


def _replace_flyer_targets(
    sb,
    *,
    flyer_id: str,
    supermarket_ids: list[str],
) -> None:
    sb.table("flyer_targets").delete().eq("flyer_id", flyer_id).execute()
    if supermarket_ids:
        sb.table("flyer_targets").insert(
            [
                {"flyer_id": flyer_id, "supermarket_id": supermarket_id}
                for supermarket_id in supermarket_ids
            ]
        ).execute()


def _file_ext(content_type: str) -> str:
    extensions = {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    return extensions[content_type]


def _file_type(content_type: str) -> str:
    return "pdf" if content_type == "application/pdf" else "image"


def _flyer_storage_reference(storage_path: str) -> str:
    """Persist a bucket-relative reference; the flyers bucket is private."""
    return storage_path


def _remove_flyer_object(sb, storage_path: str) -> None:
    try:
        sb.storage.from_("flyers").remove([storage_path])
    except Exception:
        pass


def _flyer_storage_path(flyer: dict) -> str | None:
    file_reference = str(flyer.get("file_url") or "")
    if file_reference and "://" not in file_reference:
        return file_reference
    markers = (
        "/storage/v1/object/public/flyers/",
        "/storage/v1/object/sign/flyers/",
    )
    for marker in markers:
        if marker in file_reference:
            return file_reference.split(marker, maxsplit=1)[1].split("?", maxsplit=1)[0]
    return None


def _save_flyer_preview(
    sb, *, flyer_id: str, content: bytes, content_type: str
) -> str | None:
    preview = render_flyer_preview(content, content_type)
    if preview is None:
        return None
    preview_path = f"previews/{flyer_id}.webp"
    sb.storage.from_("flyers").upload(
        path=preview_path,
        file=preview,
        file_options={"content-type": "image/webp", "upsert": "true"},
    )
    sb.table("flyers").update({"preview_path": preview_path}).eq("id", flyer_id).execute()
    return preview_path


def _ensure_flyer_preview(sb, flyer: dict) -> str | None:
    if flyer.get("preview_path"):
        return str(flyer["preview_path"])
    storage_path = _flyer_storage_path(flyer)
    if storage_path is None:
        return None
    content = bytes(sb.storage.from_("flyers").download(storage_path))
    return _save_flyer_preview(
        sb,
        flyer_id=flyer["id"],
        content=content,
        content_type="application/pdf" if flyer.get("file_type") == "pdf" else "image/png",
    )


def _private_flyer_download_access(sb, flyer: dict, user_id: str | None) -> None:
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication required")
    profile_result = (
        sb.table("user_profiles").select("*").eq("id", user_id).maybe_single().execute()
    )
    if not profile_result or not profile_result.data:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    profile = profile_result.data
    profile["managed_supermarket_ids"] = (
        _profile_supermarket_ids(profile) or _manager_supermarket_ids(sb, user_id)
    )
    if profile.get("role") not in {"admin", "supermarket_manager"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    _assert_flyer_access(sb, profile, flyer)


def _manager_supermarket_ids(sb, user_id: str) -> list[str]:
    result = sb.table("manager_supermarkets").select("supermarket_id").eq("user_id", user_id).execute()
    rows = result.data or []
    return [row["supermarket_id"] for row in rows if row.get("supermarket_id")]


def _is_public_flyer_file(sb, flyer: dict) -> bool:
    return bool(
        flyer.get("is_public")
        and flyer.get("status") == "done"
        and _has_confirmed_offers(sb, flyer["id"])
    )


def _assert_flyer_file_access(sb, flyer: dict, user_id: str | None) -> None:
    if not _is_public_flyer_file(sb, flyer):
        _private_flyer_download_access(sb, flyer, user_id)


def _normalize_requested_supermarkets(
    profile: dict,
    supermarket_ids: list[str],
    supermarket_id: str | None = None,
) -> list[str]:
    requested = list(supermarket_ids)
    if supermarket_id:
        requested.append(supermarket_id)
    return _resolve_upload_supermarket_ids(profile, list(dict.fromkeys(requested)))


def _assert_upload_file_type(content_type: str) -> None:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported file type: {content_type}",
        )


def _matches_file_signature(content: bytes, content_type: str) -> bool:
    signatures = {
        "application/pdf": lambda: content.startswith(b"%PDF-"),
        "image/jpeg": lambda: content.startswith(b"\xff\xd8\xff"),
        "image/png": lambda: content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": lambda: content[:4] == b"RIFF" and content[8:12] == b"WEBP",
        "image/gif": lambda: content.startswith((b"GIF87a", b"GIF89a")),
    }
    return signatures[content_type]()


def _assert_file_signature(content: bytes, content_type: str) -> None:
    if not _matches_file_signature(content, content_type):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File content does not match the declared content type",
        )


def _signed_upload_value(signed: object, key: str) -> str:
    if isinstance(signed, dict):
        return str(signed[key])
    return str(getattr(signed, key))


def _insert_source_flyer(sb, payload: dict) -> dict:
    row = sb.table("flyers").insert(payload).execute()
    return row.data[0]


def _insert_flyer_targets(sb, flyer_id: str, supermarket_ids: list[str]) -> None:
    sb.table("flyer_targets").insert(
        [
            {"flyer_id": flyer_id, "supermarket_id": supermarket_id}
            for supermarket_id in supermarket_ids
        ]
    ).execute()


def _build_rejected_targets(sb, requested_ids: list[str], conflicts: set[str]) -> list[dict]:
    names = _supermarket_name_map(sb, list(conflicts))
    return [
        {
            "supermarket_id": supermarket_id,
            "supermarket_name": names.get(supermarket_id, supermarket_id),
        }
        for supermarket_id in requested_ids
        if supermarket_id in conflicts
    ]


def _create_uploaded_flyer(
    sb,
    *,
    user_id: str,
    requested_ids: list[str],
    file_hash: str,
    file_url: str,
    file_type: str,
    file_name: str | None,
    valid_from: str | None,
    valid_to: str | None,
) -> dict:
    conflicts = _duplicate_target_conflicts(
        sb,
        file_hash=file_hash,
        supermarket_ids=requested_ids,
    )
    accepted_ids = [value for value in requested_ids if value not in conflicts]
    if not accepted_ids:
        names = _supermarket_name_map(sb, requested_ids)
        blocked = [
            names.get(supermarket_id, supermarket_id)
            for supermarket_id in requested_ids
        ]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Flyer already exists for: {', '.join(blocked)}",
        )

    names = _supermarket_name_map(sb, accepted_ids)
    source_flyer = _insert_source_flyer(
        sb,
        {
            "user_id": user_id,
            "supermarket_name": names.get(accepted_ids[0]),
            "supermarket_id": accepted_ids[0],
            "file_url": file_url,
            "file_type": file_type,
            "file_name": file_name,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "status": "pending",
            "is_public": False,
            "file_hash": file_hash,
            "flyer_kind": "source",
        },
    )
    _insert_flyer_targets(sb, source_flyer["id"], accepted_ids)
    enriched = _enrich_flyer(sb, source_flyer)
    enriched["rejected_targets"] = _build_rejected_targets(sb, requested_ids, conflicts)
    return enriched


def _sync_flyer_validity(
    sb,
    *,
    source_flyer_id: str,
    valid_from: str | None,
    valid_to: str | None,
) -> None:
    flyer_update = {"valid_from": valid_from, "valid_to": valid_to}
    sb.table("flyers").update(flyer_update).eq("id", source_flyer_id).execute()
    sb.table("offers").update(flyer_update).eq("flyer_id", source_flyer_id).execute()

    published_targets_resp = (
        sb.table("flyers")
        .select("id")
        .eq("source_flyer_id", source_flyer_id)
        .eq("flyer_kind", "published_target")
        .execute()
    )
    for row in published_targets_resp.data or []:
        published_flyer_id = row.get("id")
        if not published_flyer_id:
            continue
        sb.table("flyers").update(flyer_update).eq("id", published_flyer_id).execute()
        sb.table("offers").update(flyer_update).eq("flyer_id", published_flyer_id).execute()
        reschedule_flyer_published(
            sb,
            flyer_id=published_flyer_id,
            valid_from=valid_from,
        )


def _upload_product_image_to_storage(
    sb,
    *,
    storage_prefix: str,
    file_content: bytes,
    content_type: str,
) -> str:
    storage_path = f"{storage_prefix}/{uuid.uuid4()}.{_file_ext(content_type)}"
    sb.storage.from_("product-images").upload(
        path=storage_path,
        file=file_content,
        file_options={
            "content-type": content_type,
            "cache-control": "31536000",
            "upsert": "false",
        },
    )
    return sb.storage.from_("product-images").get_public_url(storage_path)


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip().lower()
    return normalized or None


def _manager_can_access_flyer(sb, profile: dict, flyer: dict) -> bool:
    if profile.get("role") != "supermarket_manager":
        return True

    managed_ids = set(_profile_supermarket_ids(profile))
    if flyer.get("supermarket_id") in managed_ids:
        return True

    if (flyer.get("flyer_kind") or "source") == "source":
        target_ids = _manager_target_ids(sb, flyer["id"])
        return bool(target_ids.intersection(managed_ids))

    return False


def _assert_flyer_access(sb, profile: dict, flyer: dict) -> None:
    if not _manager_can_access_flyer(sb, profile, flyer):
        assert_flyer_access(profile, flyer)


def _flyer_target_supermarkets(sb, supermarket_ids: list[str] | None) -> list[dict]:
    if supermarket_ids == []:
        return []
    query = (
        sb.table("supermarkets")
        .select(PUBLIC_DISCOVERY_SUPERMARKET_SELECT)
        .eq("is_active", True)
    )
    if supermarket_ids is not None:
        query = query.in_("id", supermarket_ids)
    return [public_supermarket(row) for row in (query.order("name").execute().data or [])]


@router.get("/targets")
async def list_flyer_targets(
    profile: dict = Depends(require_admin_or_manager),
) -> list[dict]:
    """Return active supermarket branches eligible as flyer targets."""
    ids = (
        None
        if profile.get("role") == "admin"
        else _profile_supermarket_ids(profile)
    )
    return _flyer_target_supermarkets(get_supabase(), ids)


@router.get("")
async def list_flyers(
    profile: dict = Depends(require_admin_or_manager),
) -> list[dict]:
    """Return flyers for admin/manager. Managers see only their supermarket's flyers."""
    sb = get_supabase()
    query = (
        sb.table("flyers")
        .select("*")
        .eq("flyer_kind", "source")
        .order("created_at", desc=True)
    )
    response = query.execute()
    raw_flyers = response.data or []
    flyer_ids = [flyer["id"] for flyer in raw_flyers if flyer.get("id")]
    draft_counts = _offer_count_by_flyer(sb, flyer_ids, is_confirmed=False)
    confirmed_counts = _offer_count_by_flyer(sb, flyer_ids, is_confirmed=True)
    published_target_counts = _published_target_count_by_source_flyer(sb, flyer_ids)
    flyers = [
        _enrich_flyer(
            sb,
            flyer,
            source_draft_count=draft_counts.get(flyer["id"], 0),
            source_confirmed_count=confirmed_counts.get(flyer["id"], 0),
            published_target_count=published_target_counts.get(flyer["id"], 0),
        )
        for flyer in raw_flyers
    ]
    if profile.get("role") != "supermarket_manager":
        return flyers
    return [flyer for flyer in flyers if _manager_can_access_flyer(sb, profile, flyer)]


@router.get("/public")
async def list_public_flyers(
    user_id: str | None = Depends(get_optional_user_id),
    request: Request = None,
) -> list[dict]:
    """Return current public flyers inside the caller's active radius."""
    sb = get_supabase()
    flyers, distances = _public_flyer_context(sb, request, user_id)
    return _visible_public_flyers(sb, flyers, distances)


@router.get("/discovery")
async def discover_public_flyers(
    user_id: str | None = Depends(get_optional_user_id),
    request: Request = None,
) -> dict:
    """Return current public flyers and nearby supermarkets from one radius lookup."""
    sb = get_supabase()
    flyers, distances = _public_flyer_context(sb, request, user_id)
    return {
        "flyers": _visible_public_flyers(sb, flyers, distances),
        "supermarkets": _nearby_supermarket_rows(sb, distances),
    }


def _public_flyer_representation(flyer: dict) -> dict:
    """Remove storage internals from the public flyer representation."""
    return {key: value for key, value in flyer.items() if key != "file_url"}


def _interactive_packshot_bbox(value: object) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(coordinate, (int, float)) for coordinate in value):
        return None
    y_min, x_min, y_max, x_max = [float(coordinate) for coordinate in value]
    if not (0 <= y_min < y_max <= 1000 and 0 <= x_min < x_max <= 1000):
        return None
    return [y_min, x_min, y_max, x_max]


@router.get("/{flyer_id}/interactive-offers")
async def list_interactive_offers(flyer_id: str) -> dict[str, list[dict]]:
    """Return public, active offers with a valid image localization for a flyer."""
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    flyer = result.data if result else None
    today = datetime.now(timezone.utc).date()
    if not flyer or not _is_public_flyer_file(sb, flyer) or not _is_flyer_current(flyer, today):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    query = (
        sb.table("offers")
        .select(
            "id, name, brand, image_url, flyer_id, price_offer, format_label, "
            "packshot_source_page, packshot_bbox"
        )
        .eq("flyer_id", flyer_id)
        .eq("offer_kind", OFFER_KIND_PUBLISHED_TARGET)
        .eq("is_confirmed", True)
    )
    rows = apply_current_offer_window(query).execute().data or []
    items: list[dict] = []
    for row in rows:
        bbox = _interactive_packshot_bbox(row.get("packshot_bbox"))
        page = row.get("packshot_source_page")
        if bbox is None or not isinstance(page, int) or page < 1:
            continue
        items.append({
            "id": row["id"],
            "name": row.get("name") or "",
            "brand": row.get("brand"),
            "image_url": row.get("image_url"),
            "price_offer": row.get("price_offer"),
            "format_label": row.get("format_label") or "",
            "source_page": page,
            "packshot_bbox": bbox,
        })
    return {"items": items}


_SIGNED_URL_TTL = 60  # seconds
_FLYER_FILE_URL_TTL = 15 * 60
PUBLIC_PREVIEW_CACHE_CONTROL = "public, max-age=86400, s-maxage=86400, stale-while-revalidate=86400"
PRIVATE_PREVIEW_CACHE_CONTROL = "private, no-store"


def _flyer_preview_response(content: bytes, is_public: bool) -> Response:
    cache_control = (
        PUBLIC_PREVIEW_CACHE_CONTROL if is_public else PRIVATE_PREVIEW_CACHE_CONTROL
    )
    return Response(
        content=content,
        media_type="image/webp",
        headers={"Cache-Control": cache_control},
    )


def _assert_flyer_file_url_access(sb, flyer: dict, user_id: str | None) -> None:
    if _is_public_flyer_file(sb, flyer):
        if _is_flyer_current(flyer, datetime.now(timezone.utc).date()):
            return
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    _private_flyer_download_access(sb, flyer, user_id)


def _signed_flyer_file_url(sb, flyer: dict) -> str:
    storage_path = _flyer_storage_path(flyer)
    if storage_path is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cannot resolve flyer storage path",
        )
    signed = sb.storage.from_("flyers").create_signed_url(
        storage_path,
        expires_in=_FLYER_FILE_URL_TTL,
    )
    return signed["signedURL"]


def _find_flyer_or_404(sb, flyer_id: str) -> dict:
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    return result.data


@router.get("/{flyer_id}/file")
async def get_flyer_file(
    flyer_id: str,
    user_id: str | None = Depends(get_optional_user_id),
) -> Response:
    """Redirect legacy callers to Storage; never stream a file through the API."""
    sb = get_supabase()
    flyer = _find_flyer_or_404(sb, flyer_id)
    _assert_flyer_file_url_access(sb, flyer, user_id)
    return RedirectResponse(
        _signed_flyer_file_url(sb, flyer),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{flyer_id}/file-url")
async def get_flyer_file_url(
    flyer_id: str,
    user_id: str | None = Depends(get_optional_user_id),
) -> JSONResponse:
    """Issue a short-lived direct Storage URL after checking flyer visibility."""
    sb = get_supabase()
    flyer = _find_flyer_or_404(sb, flyer_id)
    _assert_flyer_file_url_access(sb, flyer, user_id)
    return JSONResponse(
        {"file_url": _signed_flyer_file_url(sb, flyer), "expires_in": _FLYER_FILE_URL_TTL},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{flyer_id}/preview")
async def flyer_preview(
    flyer_id: str,
    user_id: str | None = Depends(get_optional_user_id),
) -> Response:
    """Return a compact flyer preview through the backend."""
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    flyer = result.data
    is_public = _is_public_flyer_file(sb, flyer)
    if not is_public:
        _private_flyer_download_access(sb, flyer, user_id)
    preview_path = _ensure_flyer_preview(sb, flyer)
    if preview_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer preview unavailable")
    content = bytes(sb.storage.from_("flyers").download(preview_path))
    return _flyer_preview_response(content, is_public)


@router.get("/{flyer_id}/preview-url")
async def flyer_preview_url(
    flyer_id: str,
    user_id: str | None = Depends(get_optional_user_id),
) -> dict[str, str]:
    """Return a short-lived preview URL for authenticated private workflows."""
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    flyer = result.data
    _assert_flyer_file_access(sb, flyer, user_id)
    preview_path = _ensure_flyer_preview(sb, flyer)
    if preview_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer preview unavailable")
    signed = sb.storage.from_("flyers").create_signed_url(preview_path, expires_in=_SIGNED_URL_TTL)
    return {"preview_url": signed["signedURL"]}


@router.post("/upload-url")
async def create_flyer_upload_url(
    payload: FlyerSignedUploadRequest,
    user_id: str = Depends(get_current_user_id),
    profile: dict = Depends(require_admin_or_manager),
) -> FlyerSignedUploadResponse:
    """Return a signed Storage upload target for a flyer source file."""
    _assert_upload_file_type(payload.content_type)
    requested_ids = _normalize_requested_supermarkets(profile, payload.supermarket_ids)
    if not requested_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one supermarket",
        )
    storage_path = f"{user_id}/{uuid.uuid4()}.{_file_ext(payload.content_type)}"
    signed = get_supabase().storage.from_("flyers").create_signed_upload_url(storage_path)
    return FlyerSignedUploadResponse(
        bucket="flyers",
        path=storage_path,
        token=_signed_upload_value(signed, "token"),
        signed_url=_signed_upload_value(signed, "signed_url"),
    )


@router.post("/upload/complete", status_code=status.HTTP_201_CREATED)
async def complete_flyer_upload(
    payload: FlyerUploadCompleteRequest,
    user_id: str = Depends(get_current_user_id),
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Validate uploaded Storage object and create pending flyer source."""
    _assert_upload_file_type(payload.content_type)
    if not payload.storage_path.startswith(f"{user_id}/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid flyer storage path",
        )

    requested_ids = _normalize_requested_supermarkets(profile, payload.supermarket_ids)
    if not requested_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one supermarket",
        )

    sb = get_supabase()
    content = bytes(sb.storage.from_("flyers").download(payload.storage_path))
    if len(content) > MAX_FILE_SIZE:
        _remove_flyer_object(sb, payload.storage_path)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds 50 MB limit",
        )
    _assert_file_signature(content, payload.content_type)

    file_hash = hashlib.sha256(content).hexdigest()
    try:
        flyer = _create_uploaded_flyer(
            sb,
            user_id=user_id,
            requested_ids=requested_ids,
            file_hash=file_hash,
            file_url=_flyer_storage_reference(payload.storage_path),
            file_type=_file_type(payload.content_type),
            file_name=payload.file_name,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )
        preview_path = _save_flyer_preview(
            sb,
            flyer_id=flyer["id"],
            content=content,
            content_type=payload.content_type,
        )
        if preview_path is not None:
            flyer["preview_path"] = preview_path
        return flyer
    except HTTPException:
        _remove_flyer_object(sb, payload.storage_path)
        raise


@router.get("/{flyer_id}")
async def get_flyer(
    flyer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Fetch a single flyer by ID. Managers can only access their supermarket's flyers."""
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    flyer = result.data
    _assert_flyer_access(sb, profile, flyer)
    return _enrich_flyer(
        sb,
        flyer,
        source_draft_count=_offer_count_by_flyer(sb, [flyer_id], is_confirmed=False).get(flyer_id, 0),
        source_confirmed_count=_offer_count_by_flyer(sb, [flyer_id], is_confirmed=True).get(flyer_id, 0),
        published_target_count=_published_target_count_by_source_flyer(sb, [flyer_id]).get(flyer_id, 0),
    )


@router.patch("/{flyer_id}")
async def update_flyer_validity(
    flyer_id: str,
    payload: FlyerValidityUpdate,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    flyer = result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)

    _sync_flyer_validity(
        sb,
        source_flyer_id=flyer_id,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
    )
    updated = sb.table("flyers").select("*").eq("id", flyer_id).single().execute().data
    return _enrich_flyer(sb, updated)


@router.get("/{flyer_id}/targets")
async def get_flyer_targets(
    flyer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> list[dict]:
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    flyer = result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)
    return _flyer_targets(sb, flyer_id)


@router.put("/{flyer_id}/targets")
@router.patch("/{flyer_id}/targets")
async def update_flyer_targets(
    flyer_id: str,
    payload: FlyerTargetsUpdate,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    flyer = result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)
    if flyer.get("is_public"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot modify targets after publication",
        )

    requested_ids = _resolve_upload_supermarket_ids(profile, payload.supermarket_ids)
    conflicts = _duplicate_target_conflicts(
        sb,
        file_hash=flyer.get("file_hash"),
        supermarket_ids=requested_ids,
        exclude_source_flyer_id=flyer_id,
    )
    accepted_ids = [value for value in requested_ids if value not in conflicts]
    if not accepted_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="All selected supermarkets already have this flyer",
        )

    _replace_flyer_targets(sb, flyer_id=flyer_id, supermarket_ids=accepted_ids)
    name_map = _supermarket_name_map(sb, accepted_ids)
    first_target_id = accepted_ids[0]
    sb.table("flyers").update(
        {
            "supermarket_id": first_target_id,
            "supermarket_name": name_map.get(first_target_id),
        }
    ).eq("id", flyer_id).execute()

    updated = sb.table("flyers").select("*").eq("id", flyer_id).single().execute().data
    targets = _flyer_targets(sb, flyer_id)
    source_offers = _source_master_offers(sb, flyer_id)
    if source_offers or _published_target_flyers(sb, flyer_id):
        _sync_published_targets_for_source_flyer(
            sb,
            source_flyer=updated,
            targets=targets,
            notify_new=True,
            source_offers=source_offers,
        )
    enriched = _enrich_flyer(sb, updated)
    rejected_names = _supermarket_name_map(sb, list(conflicts))
    enriched["rejected_targets"] = [
        {
            "supermarket_id": supermarket_id,
            "supermarket_name": rejected_names.get(supermarket_id, supermarket_id),
        }
        for supermarket_id in requested_ids
        if supermarket_id in conflicts
    ]
    return enriched


@router.delete("/{flyer_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_flyer(
    flyer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> None:
    """Delete a flyer immediately: removes storage file (best-effort) and DB row."""
    sb = get_supabase()
    result = sb.table("flyers").select("id, file_url, supermarket_id, supermarket_name").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    flyer = result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)

    storage_paths = [
        value
        for value in (_flyer_storage_path(flyer), flyer.get("preview_path"))
        if value
    ]
    for storage_path in storage_paths:
        _remove_flyer_object(sb, storage_path)

    sb.table("flyers").delete().eq("id", flyer_id).execute()


@router.post("/{flyer_id}/extract", status_code=status.HTTP_202_ACCEPTED)
async def trigger_extraction(
    flyer_id: str,
    background_tasks: BackgroundTasks,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Trigger AI extraction for a pending or errored flyer."""
    sb = get_supabase()
    result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    flyer = result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)

    resumable_processing = _can_resume_stale_processing(flyer)
    allowed_statuses = {"pending", "error"}
    if flyer.get("status") not in allowed_statuses and not resumable_processing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot trigger extraction: flyer status is '{flyer.get('status')}'",
        )

    sb.table("flyers").update({"status": "processing", "error_message": None}).eq("id", flyer_id).execute()

    from services.extraction.service import ExtractionService
    background_tasks.add_task(ExtractionService().run, flyer_id)

    return {"status": "processing", "flyer_id": flyer_id}


@router.get("/{flyer_id}/draft-offers")
async def list_draft_offers(
    flyer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> list[dict]:
    """Return all unconfirmed offers for a flyer."""
    sb = get_supabase()
    result = sb.table("flyers").select("id, supermarket_id, supermarket_name, flyer_kind").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    _source_flyer_required(result.data)
    _assert_flyer_access(sb, profile, result.data)

    offers_resp = (
        sb.table("offers")
        .select(_OFFER_PRODUCT_SELECT)
        .eq("flyer_id", flyer_id)
        .eq("is_confirmed", False)
        .execute()
    )
    return [_flatten_draft_offer(o) for o in (offers_resp.data or [])]


@router.get("/{flyer_id}/confirmed-offers")
async def list_confirmed_offers(
    flyer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> list[dict]:
    """Return all confirmed offers for a flyer."""
    sb = get_supabase()
    result = sb.table("flyers").select("id, supermarket_id, supermarket_name, flyer_kind").eq("id", flyer_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    _source_flyer_required(result.data)
    _assert_flyer_access(sb, profile, result.data)

    offers_resp = (
        sb.table("offers")
        .select(_OFFER_PRODUCT_SELECT)
        .eq("flyer_id", flyer_id)
        .eq("is_confirmed", True)
        .eq("offer_kind", OFFER_KIND_SOURCE_MASTER)
        .execute()
    )
    return [_flatten_draft_offer(o) for o in (offers_resp.data or [])]


@router.post("/{flyer_id}/draft-offers", status_code=status.HTTP_201_CREATED)
async def create_draft_offer(
    flyer_id: str,
    payload: DraftOfferCreate,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Manually add a draft offer to a flyer."""
    sb = get_supabase()
    flyer_result = (
        sb.table("flyers")
        .select("id, supermarket_id, supermarket_name, valid_from, valid_to, flyer_kind")
        .eq("id", flyer_id)
        .maybe_single()
        .execute()
    )
    if not flyer_result or not flyer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")
    flyer = flyer_result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)

    normalized_unit = normalize_unit_price_measure(payload.unit_price_unit) if payload.unit_price_unit else None
    offer_row = build_offer_row(
        payload, flyer["supermarket_id"], flyer.get("supermarket_name"),
        flyer_id, normalized_unit, format_fields=build_format_fields(payload),
    )
    # Apply flyer date fallback (flyers.py-specific concern)
    offer_row["valid_from"] = offer_row["valid_from"] or flyer.get("valid_from")
    offer_row["valid_to"] = offer_row["valid_to"] or flyer.get("valid_to")
    return insert_and_fetch_offer(sb, offer_row)


@router.patch("/{flyer_id}/draft-offers/{offer_id}")
async def update_draft_offer(
    flyer_id: str,
    offer_id: str,
    payload: DraftOfferUpdate,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Inline-edit a single draft offer (and its canonical product if needed)."""
    sb = get_supabase()
    flyer_result = sb.table("flyers").select("id, supermarket_id, supermarket_name, flyer_kind").eq("id", flyer_id).maybe_single().execute()
    if not flyer_result or not flyer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    _source_flyer_required(flyer_result.data)
    _assert_flyer_access(sb, profile, flyer_result.data)

    offer_result = (
        sb.table("offers")
        .select("id, flyer_id, is_confirmed")
        .eq("id", offer_id)
        .eq("flyer_id", flyer_id)
        .maybe_single()
        .execute()
    )
    if not offer_result or not offer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    offer = offer_result.data
    sent = payload.model_fields_set
    offer_fields = {
        k: (normalize_unit_price_measure(v) if k == "unit_price_unit" else v)
        for k, v in {
            "price_offer": payload.price_offer,
            "price_original": payload.price_original,
            "unit_price_value": payload.unit_price_value,
            "unit_price_unit": payload.unit_price_unit,
            "offer_notes": payload.offer_notes,
            "is_reviewed": payload.is_reviewed,
        }.items()
        if k in sent
    }
    if "unit_price_value" in offer_fields and "unit_price_unit" in offer_fields:
        offer_fields["unit_price"] = format_unit_price_label(
            offer_fields["unit_price_value"],
            offer_fields["unit_price_unit"],
        )
    if "format" in sent and payload.format is not None:
        format_bundle = build_format_bundle(payload.format.model_dump(mode="json"))
        offer_fields["format"] = format_bundle.format_compact
        offer_fields["format_key"] = format_bundle.format_key
        offer_fields["format_label"] = format_bundle.format_label
    if offer_fields:
        sb.table("offers").update(offer_fields).eq("id", offer_id).execute()

    product_payload = {
        "name": payload.name,
        "brand": payload.brand,
        "category": payload.category,
        "subcategory": payload.subcategory,
    }
    product_fields = {k: v for k, v in product_payload.items() if k in sent}
    if product_fields:
        draft_fields = dict(product_fields)
        draft_fields["offer_key"] = draft_product_key(
            payload.name if "name" in sent else None,
            payload.brand if "brand" in sent else None,
        )
        if "name" not in sent or "brand" not in sent:
            current = (
                sb.table("offers")
                .select("name, brand")
                .eq("id", offer_id)
                .single()
                .execute()
            )
            current_data = current.data or {}
            draft_fields["draft_product_key"] = draft_product_key(
                payload.name if "name" in sent else current_data.get("name"),
                payload.brand if "brand" in sent else current_data.get("brand"),
            )
        sb.table("offers").update(draft_fields).eq("id", offer_id).execute()

    updated = (
        sb.table("offers")
        .select(_OFFER_PRODUCT_SELECT)
        .eq("id", offer_id)
        .single()
        .execute()
    )
    updated_offer = updated.data
    if updated_offer.get("is_confirmed") and _offer_kind(updated_offer) == OFFER_KIND_SOURCE_MASTER:
        _sync_published_clones_for_source_offer(
            sb,
            source_offer=updated_offer,
            target_flyers=_published_target_flyers(sb, flyer_id),
        )
        updated = (
            sb.table("offers")
            .select(_OFFER_PRODUCT_SELECT)
            .eq("id", offer_id)
            .single()
            .execute()
        )
    return _flatten_draft_offer(updated.data)


@router.post("/{flyer_id}/draft-offers/{offer_id}/image")
async def upload_draft_offer_image(
    flyer_id: str,
    offer_id: str,
    file: Annotated[UploadFile, File()],
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Upload a staged product image for a draft offer that will create a new product."""
    sb = get_supabase()
    flyer_result = (
        sb.table("flyers")
        .select("id, supermarket_id, supermarket_name, flyer_kind")
        .eq("id", flyer_id)
        .maybe_single()
        .execute()
    )
    if not flyer_result or not flyer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    _source_flyer_required(flyer_result.data)
    _assert_flyer_access(sb, profile, flyer_result.data)

    offer_result = (
        sb.table("offers")
        .select("id, flyer_id, is_confirmed")
        .eq("id", offer_id)
        .eq("flyer_id", flyer_id)
        .maybe_single()
        .execute()
    )
    if not offer_result or not offer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    offer = offer_result.data
    if offer.get("is_confirmed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Confirmed offers must be updated from the catalog product page",
        )
    if not file.content_type or file.content_type not in ALLOWED_PRODUCT_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Formato immagine non supportato. Usa JPEG, PNG, WebP o GIF.",
        )

    content = await file.read()
    if len(content) > MAX_PRODUCT_IMAGE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Immagine troppo grande. Max 10 MB.",
        )
    _assert_file_signature(content, file.content_type)

    public_url = _upload_product_image_to_storage(
        sb,
        storage_prefix=f"draft-offers/{offer_id}",
        file_content=content,
        content_type=file.content_type,
    )
    sb.table("offers").update({"image_url": public_url}).eq("id", offer_id).execute()

    updated = (
        sb.table("offers")
        .select(_OFFER_PRODUCT_SELECT)
        .eq("id", offer_id)
        .single()
        .execute()
    )
    return _flatten_draft_offer(updated.data)


@router.delete("/{flyer_id}/draft-offers/{offer_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_draft_offer(
    flyer_id: str,
    offer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> None:
    """Delete a source offer and any published clones derived from it."""
    sb = get_supabase()
    flyer_result = sb.table("flyers").select("id, supermarket_id, supermarket_name, flyer_kind").eq("id", flyer_id).maybe_single().execute()
    if not flyer_result or not flyer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    _source_flyer_required(flyer_result.data)
    _assert_flyer_access(sb, profile, flyer_result.data)

    offer_result = (
        sb.table("offers")
        .select("id, flyer_id, is_confirmed")
        .eq("id", offer_id)
        .eq("flyer_id", flyer_id)
        .maybe_single()
        .execute()
    )
    if not offer_result or not offer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    offer = offer_result.data
    if offer.get("is_confirmed"):
        sb.table("offers").delete().eq("source_offer_id", offer_id).execute()
    sb.table("offers").delete().eq("id", offer_id).execute()


@router.post("/{flyer_id}/offers/confirm")
async def confirm_offers(
    flyer_id: str,
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Confirm source flyer offers and publish one derived flyer per target."""
    sb = get_supabase()
    flyer_result = sb.table("flyers").select("*").eq("id", flyer_id).maybe_single().execute()
    if not flyer_result or not flyer_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flyer not found")

    flyer = flyer_result.data
    _source_flyer_required(flyer)
    _assert_flyer_access(sb, profile, flyer)

    if flyer.get("status") != "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot confirm offers: flyer status is '{flyer.get('status')}' (must be 'done')",
        )

    targets = _flyer_targets(sb, flyer_id)
    if not targets:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Add at least one supermarket target before confirmation",
        )

    drafts = (
        sb.table("offers")
        .select("*")
        .eq("flyer_id", flyer_id)
        .eq("is_confirmed", False)
        .execute()
    )
    draft_rows = drafts.data or []

    if draft_rows:
        sb.table("offers").update(
            {"is_confirmed": True, "offer_kind": OFFER_KIND_SOURCE_MASTER}
        ).eq("flyer_id", flyer_id).eq("is_confirmed", False).execute()
    confirmed_count = len(draft_rows)

    source_confirmed = (
        sb.table("offers")
        .select("*", count="exact")
        .eq("flyer_id", flyer_id)
        .eq("is_confirmed", True)
        .eq("offer_kind", OFFER_KIND_SOURCE_MASTER)
        .execute()
    )
    source_offers = source_confirmed.data or []
    source_offer_count = (
        source_confirmed.count
        if isinstance(source_confirmed.count, int)
        else len(source_offers)
    )
    if source_offer_count <= 0:
        return {
            "confirmed": confirmed_count,
            "flyer_id": flyer_id,
            "published_flyers": [],
        }

    existing_target_flyer_by_supermarket = {
        supermarket_id: target["flyer_id"]
        for supermarket_id, target in _published_target_flyers(sb, flyer_id).items()
    }

    published_flyers: list[dict] = []
    published_flyer_ids: list[str] = []
    for target in targets:
        target_supermarket_id = target["supermarket_id"]
        target_supermarket_name = target.get("supermarket_name") or "Supermercato"
        published_flyer_id = existing_target_flyer_by_supermarket.get(target_supermarket_id)

        if not published_flyer_id:
            inserted = (
                sb.table("flyers")
                .insert(
                    {
                        "user_id": flyer.get("user_id"),
                        "supermarket_id": target_supermarket_id,
                        "supermarket_name": target_supermarket_name,
                        "file_url": flyer.get("file_url"),
                        "file_type": flyer.get("file_type"),
                        "file_name": flyer.get("file_name"),
                        "preview_path": flyer.get("preview_path"),
                        "valid_from": flyer.get("valid_from"),
                        "valid_to": flyer.get("valid_to"),
                        "status": "done",
                        "error_message": None,
                        "products_count": 0,
                        "pages_count": flyer.get("pages_count"),
                        "extraction_metadata": flyer.get("extraction_metadata"),
                        "is_public": True,
                        "file_hash": flyer.get("file_hash"),
                        "flyer_kind": "published_target",
                        "source_flyer_id": flyer_id,
                    }
                )
                .execute()
            )
            published_flyer_id = inserted.data[0]["id"]
            if draft_rows and not flyer.get("is_public"):
                enqueue_flyer_published(
                    sb,
                    flyer_id=published_flyer_id,
                    supermarket_id=target_supermarket_id,
                    supermarket_name=target_supermarket_name,
                    products_count=source_offer_count,
                    valid_from=flyer.get("valid_from"),
                )
        else:
            sb.table("flyers").update(
                {
                    "supermarket_name": target_supermarket_name,
                    "status": "done",
                    "valid_from": flyer.get("valid_from"),
                    "valid_to": flyer.get("valid_to"),
                    "is_public": True,
                }
            ).eq("id", published_flyer_id).execute()

        published_flyers.append(
            {
                "flyer_id": published_flyer_id,
                "supermarket_id": target_supermarket_id,
                "supermarket_name": target_supermarket_name,
            }
        )
        published_flyer_ids.append(published_flyer_id)

    target_flyers = _published_target_flyers(sb, flyer_id)
    published_counts = _sync_published_clones_for_source_offers(
        sb,
        source_offers=source_offers,
        target_flyers=target_flyers,
    )
    for published_flyer_id in published_flyer_ids:
        sb.table("flyers").update(
            {"products_count": published_counts.get(published_flyer_id, 0)}
        ).eq("id", published_flyer_id).execute()
    return {
        "confirmed": confirmed_count,
        "flyer_id": flyer_id,
        "published_flyers": published_flyers,
    }


@router.post("/admin/cleanup", status_code=200)
async def trigger_flyer_cleanup(
    profile: dict = Depends(require_admin_or_manager),
) -> dict:
    """Manually trigger expired-flyer cleanup. Admin only. For ops and testing."""
    if profile.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    from services.flyer_cleanup import FlyerCleanupService
    deleted = FlyerCleanupService().run()
    return {"deleted": deleted}
