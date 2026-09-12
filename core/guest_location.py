"""Signed, short-lived location state for unauthenticated discovery."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from core.config import settings
from core.session import create_session_token, read_session_token

GUEST_LOCATION_COOKIE = "girospesa_guest_location"
GUEST_LOCATION_TYPE = "guest_location"
GUEST_LOCATION_RADIUS_KM = 10.0
GUEST_LOCATION_TTL_SECONDS = 60 * 60 * 24 * 30


def create_guest_location_token(municipality_code: str) -> str:
    return create_session_token(
        {
            "typ": GUEST_LOCATION_TYPE,
            "municipality_code": municipality_code,
            "radius": GUEST_LOCATION_RADIUS_KM,
        },
        lifetime_seconds=GUEST_LOCATION_TTL_SECONDS,
    )


def read_guest_location(token: str | None) -> tuple[str, float] | None:
    if not token:
        return None
    claims = read_session_token(token)
    if not claims or claims.get("typ") != GUEST_LOCATION_TYPE:
        return None
    return _location_from_claims(claims)


def _location_from_claims(claims: dict[str, Any]) -> tuple[str, float] | None:
    municipality_code = claims.get("municipality_code")
    radius = claims.get("radius")
    if not isinstance(municipality_code, str) or not municipality_code.isdigit():
        return None
    if len(municipality_code) != 6 or radius != GUEST_LOCATION_RADIUS_KM:
        return None
    return municipality_code, float(radius)


def cookie_secure(origin: str | None = None) -> bool:
    if origin:
        return origin.startswith("https://")
    return settings.environment.lower() not in {"development", "test"}


def cookie_samesite(origin: str | None = None) -> str:
    return "none" if cookie_secure(origin) else "lax"


def guest_location_required(clear_cookie: bool) -> HTTPException:
    headers = {"Cache-Control": "no-store"}
    if clear_cookie:
        same_site = "None" if cookie_secure() else "Lax"
        cookie = f"{GUEST_LOCATION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite={same_site}"
        headers["Set-Cookie"] = f"{cookie}; Secure" if cookie_secure() else cookie
    return HTTPException(428, detail={"code": "guest_location_required"}, headers=headers)
