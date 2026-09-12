import hashlib
import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict

from core.auth import get_optional_user_id, require_admin
from core.database import get_supabase
from core.guest_location import GUEST_LOCATION_COOKIE, guest_location_required, read_guest_location
from api.routers._nearby_supermarkets import (
    PUBLIC_DISCOVERY_SUPERMARKET_SELECT,
    active_nearby_supermarkets,
    nearby_supermarket_distances,
    public_supermarket as serialize_public_supermarket,
    request_location,
)
from services.municipalities import MunicipalityNotFoundError, get_municipality

router = APIRouter()


class SupermarketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    municipality_code: str | None = None

ALLOWED_LOGO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_LOGO_SIZE = 2 * 1024 * 1024  # 2 MB
LOGO_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
LOGO_CACHE_CONTROL = "31536000"


def _merge_distances(
    rows: list[dict], distances: dict[str, float | None], municipality_code: str
) -> list[dict]:
    merged = [
        {**_public_supermarket(row), "distance_km": distances[row["id"]]}
        for row in rows
        if row["id"] in distances
    ]
    positions = {supermarket_id: index for index, supermarket_id in enumerate(distances)}
    return sorted(
        merged,
        key=lambda row: (row.get("municipality_code") != municipality_code, positions[row["id"]]),
    )


def _make_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    return slug.strip("-")


def _unique_slug(sb, base: str) -> str:
    slug, i = base, 2
    while sb.table("supermarkets").select("id").eq("slug", slug).execute().data:
        slug, i = f"{base}-{i}", i + 1
    return slug


def _branch_municipality_code(sb, code: str | None) -> str:
    if not code:
        raise HTTPException(status_code=422, detail="Seleziona un Comune dall'elenco")
    try:
        get_municipality(sb, code)
    except MunicipalityNotFoundError:
        raise HTTPException(status_code=422, detail="Seleziona un Comune dall'elenco")
    return code


def _public_supermarket(supermarket: dict) -> dict:
    return serialize_public_supermarket(supermarket)


@router.get("")
async def list_supermarkets(
    with_active_offers: bool = Query(False),
    request: Request = None,
    user_id: str | None = Depends(get_optional_user_id),
) -> list[dict]:
    """Return active supermarkets inside caller's active radius."""
    sb = get_supabase()
    guest_token = request.cookies.get(GUEST_LOCATION_COOKIE) if user_id is None else None
    guest_location = read_guest_location(guest_token)
    if user_id is None and guest_location is None:
        raise guest_location_required(clear_cookie=guest_token is not None)
    location = request_location(sb, user_id, guest_location)
    if location is not None:
        distances = nearby_supermarket_distances(
            sb, location.municipality_code, location.max_distance_km
        )
        ids = list(distances)
        if not ids:
            return []
        if with_active_offers:
            return active_nearby_supermarkets(sb, distances, location.municipality_code)
        nearby_rows = []
        if ids:
            resp = (
                sb.table("supermarkets")
                .select(PUBLIC_DISCOVERY_SUPERMARKET_SELECT)
                .in_("id", ids)
                .execute()
            )
            nearby_rows = _merge_distances(
                resp.data or [], distances, location.municipality_code
            )
        return nearby_rows
    return []


def _logo_storage_path(sm_id: str, logo_content: bytes, content_type: str) -> str:
    digest = hashlib.sha256(logo_content).hexdigest()
    return f"{sm_id}/{digest}.{LOGO_EXT[content_type]}"


def _upload_logo(sb, sm_id: str, logo_content: bytes, content_type: str) -> str:
    """Upload an immutable logo asset and return its public URL."""
    storage_path = _logo_storage_path(sm_id, logo_content, content_type)
    try:
        sb.storage.from_("logos").upload(
            path=storage_path,
            file=logo_content,
            file_options={
                "content-type": content_type,
                "cache-control": LOGO_CACHE_CONTROL,
                "upsert": "true",
            },
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logo upload failed",
        )
    return sb.storage.from_("logos").get_public_url(storage_path)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_supermarket(
    name: Annotated[str, Form()],
    logo: Annotated[UploadFile, File()],
    municipality_code: Annotated[str, Form()],
    _admin: Annotated[dict, Depends(require_admin)] = None,
) -> dict:
    """Create a new supermarket branch with required logo. Admin only."""
    if not logo.content_type or logo.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported logo type: {logo.content_type}",
        )
    logo_content = await logo.read()
    if len(logo_content) > MAX_LOGO_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Logo exceeds {MAX_LOGO_SIZE // (1024 * 1024)} MB limit",
        )

    sb = get_supabase()
    slug = _unique_slug(sb, _make_slug(name))
    municipality_code = _branch_municipality_code(sb, municipality_code)
    row = {
        "name": name,
        "slug": slug,
        "municipality_code": municipality_code,
        "is_active": True,
    }
    resp = sb.table("supermarkets").insert(row).execute()
    sm = resp.data[0]
    sm_id = sm["id"]

    try:
        logo_url = _upload_logo(sb, sm_id, logo_content, logo.content_type)
    except HTTPException:
        sb.table("supermarkets").delete().eq("id", sm_id).execute()
        raise

    updated = (
        sb.table("supermarkets")
        .update({"logo_url": logo_url})
        .eq("id", sm_id)
        .execute()
    )
    return _public_supermarket(updated.data[0])


@router.patch("/{supermarket_id}")
async def update_supermarket(
    supermarket_id: str,
    body: SupermarketUpdate,
    _admin: Annotated[dict, Depends(require_admin)] = None,
) -> dict:
    """Update supermarket info fields. Admin only."""
    sb = get_supabase()
    result = (
        sb.table("supermarkets")
        .select("*")
        .eq("id", supermarket_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supermarket not found")

    existing_row = result.data
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return _public_supermarket(existing_row)

    if "municipality_code" in updates:
        updates["municipality_code"] = _branch_municipality_code(
            sb, updates["municipality_code"]
        )

    updated = (
        sb.table("supermarkets")
        .update(updates)
        .eq("id", supermarket_id)
        .execute()
    )
    return _public_supermarket(updated.data[0])


@router.patch("/{supermarket_id}/logo")
async def update_supermarket_logo(
    supermarket_id: str,
    logo: Annotated[UploadFile, File()],
    _admin: Annotated[dict, Depends(require_admin)] = None,
) -> dict:
    """Update the logo for an existing supermarket. Admin only."""
    if not logo.content_type or logo.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported logo type: {logo.content_type}",
        )
    logo_content = await logo.read()
    if len(logo_content) > MAX_LOGO_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Logo exceeds {MAX_LOGO_SIZE // (1024 * 1024)} MB limit",
        )

    sb = get_supabase()
    result = (
        sb.table("supermarkets")
        .select("id")
        .eq("id", supermarket_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supermarket not found")

    logo_url = _upload_logo(sb, supermarket_id, logo_content, logo.content_type)
    updated = (
        sb.table("supermarkets")
        .update({"logo_url": logo_url})
        .eq("id", supermarket_id)
        .execute()
    )
    return _public_supermarket(updated.data[0])
