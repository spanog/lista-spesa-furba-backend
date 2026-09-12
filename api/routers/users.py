from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from core.auth import get_current_user_id
from core.config import settings
from core.database import get_supabase
from core.supabase_client import create_supabase_client as create_client
from services.municipalities import MunicipalityNotFoundError, get_municipality

router = APIRouter()
logger = logging.getLogger(__name__)

_AVATAR_BUCKET = "avatars"
_AVATAR_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
_AVATAR_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


class UpdateProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    municipality_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    max_distance_km: int | None = Field(default=None, ge=1, le=20)
    notifications_enabled: bool | None = None


_PROFILE_SELECT = "*, municipalities(code,name,province_name,province_code)"


def _serialize_profile(profile: dict) -> dict:
    municipality = profile.pop("municipalities", None)
    return {**profile, "municipality": municipality}


def _load_profile(sb, user_id: str) -> dict | None:
    response = (
        sb.table("user_profiles")
        .select(_PROFILE_SELECT)
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    return _serialize_profile(response.data) if response.data else None


@router.get("/me")
async def get_profile(user_id: Annotated[str, Depends(get_current_user_id)]) -> dict:
    profile = _load_profile(get_supabase(), user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.put("/me")
async def update_profile(
    body: UpdateProfileBody,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    sb = get_supabase()
    update_data = body.model_dump(exclude_none=True)
    _validate_municipality_update(sb, update_data)

    sb.table("user_profiles").update(update_data).eq("id", user_id).execute()
    profile = _load_profile(sb, user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _validate_municipality_update(sb, update_data: dict) -> None:
    code = update_data.get("municipality_code")
    if code is None:
        return
    try:
        get_municipality(sb, code)
    except MunicipalityNotFoundError as error:
        raise HTTPException(status_code=422, detail="Seleziona un Comune dall'elenco") from error


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    """Upload a new avatar image; returns the public URL stored in user_profiles."""
    if file.content_type not in _AVATAR_ALLOWED_MIME:
        raise HTTPException(status_code=415, detail="Unsupported image type. Use JPEG, PNG or WebP.")

    data = await file.read()
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Image must be smaller than 5 MB.")

    ext = file.content_type.split("/")[-1].replace("jpeg", "jpg")
    path = f"{user_id}.{ext}"

    sb = get_supabase()
    sb.storage.from_(_AVATAR_BUCKET).upload(
        path,
        data,
        file_options={"content-type": file.content_type, "upsert": "true"},
    )

    raw_url = sb.storage.from_(_AVATAR_BUCKET).get_public_url(path)
    avatar_url = f"{raw_url.rstrip('?')}?t={int(time.time())}"
    sb.table("user_profiles").update({"avatar_url": avatar_url}).eq("id", user_id).execute()
    return {"avatar_url": avatar_url}


class UpdatePasswordBody(BaseModel):
    current_password: str = Field(min_length=1)
    password: str = Field(min_length=8)


@router.post("/me/password", status_code=204)
async def update_password(
    body: UpdatePasswordBody,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> Response:
    sb = get_supabase()
    try:
        user_resp = sb.auth.admin.get_user_by_id(user_id)
        email = user_resp.user.email
        verify_client = create_client(settings.supabase_url, settings.supabase_secret_key)
        verify_client.auth.sign_in_with_password({"email": email, "password": body.current_password})
    except Exception:
        raise HTTPException(status_code=400, detail="Password attuale non corretta.")
    try:
        sb.auth.admin.update_user_by_id(user_id, {"password": body.password})
    except Exception:
        raise HTTPException(status_code=400, detail="Aggiornamento password fallito.")
    return Response(status_code=204)


def _is_missing_user_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = ("user not found", "not found", "does not exist")
    return any(marker in message for marker in markers)


def _cleanup_account_delete_dependencies(sb: object, user_id: str) -> None:
    # Historical invite FKs may block auth.users deletion unless they are
    # nulled or removed before the auth record is deleted.
    sb.table("list_members").update({"invited_by": None}).eq("invited_by", user_id).execute()
    sb.table("list_invites").update({"accepted_by": None}).eq("accepted_by", user_id).execute()
    sb.table("list_invites").delete().eq("invited_by", user_id).execute()


def _delete_auth_user(user_id: str) -> None:
    sb = get_supabase()
    try:
        _cleanup_account_delete_dependencies(sb, user_id)
        sb.auth.admin.delete_user(user_id)
    except Exception as exc:
        if _is_missing_user_error(exc):
            logger.info("Account already deleted", extra={"user_id": user_id})
            return
        logger.exception("Account deletion failed", extra={"user_id": user_id})
        raise HTTPException(
            status_code=502,
            detail="Eliminazione account non riuscita. Riprova tra qualche istante.",
        ) from exc


@router.delete("/me", status_code=204)
async def delete_account(
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> Response:
    """Permanently delete the authenticated user's account and all their data."""
    _delete_auth_user(user_id)
    return Response(status_code=204)
