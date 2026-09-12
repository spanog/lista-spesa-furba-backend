"""Guest location cookie endpoints."""
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from core.guest_location import (
    GUEST_LOCATION_COOKIE,
    GUEST_LOCATION_TTL_SECONDS,
    cookie_secure,
    cookie_samesite,
    create_guest_location_token,
)
from core.database import get_supabase
from services.municipalities import MunicipalityNotFoundError, get_municipality

router = APIRouter()


class GuestLocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    municipality_code: str = Field(pattern=r"^[0-9]{6}$")


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def set_guest_location(
    body: GuestLocationBody, request: Request, response: Response
) -> None:
    origin = request.headers.get("origin")
    try:
        get_municipality(get_supabase(), body.municipality_code)
    except MunicipalityNotFoundError:
        raise HTTPException(status_code=422, detail="Seleziona un Comune dall'elenco")
    response.set_cookie(
        GUEST_LOCATION_COOKIE,
        create_guest_location_token(body.municipality_code),
        max_age=GUEST_LOCATION_TTL_SECONDS,
        httponly=True,
        secure=cookie_secure(origin),
        samesite=cookie_samesite(origin),
        path="/",
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_guest_location(request: Request, response: Response) -> None:
    origin = request.headers.get("origin")
    response.delete_cookie(
        GUEST_LOCATION_COOKIE,
        httponly=True,
        secure=cookie_secure(origin),
        samesite=cookie_samesite(origin),
        path="/",
    )
