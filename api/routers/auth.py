"""Backend auth router — signup and password recovery helpers."""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from core.config import settings
from core.database import get_supabase
from core.session import create_session_token, read_session_token
from core.supabase_client import create_supabase_client as create_client
from services.municipalities import MunicipalityNotFoundError, get_municipality

_PASSWORD_RESET_TTL_SECONDS = 60 * 60  # 1-hour recovery window

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _fresh_supabase_client():
    return create_client(settings.supabase_url, settings.supabase_secret_key)


def _auth_user_updated_at(user: object) -> str | None:
    value = getattr(user, "updated_at", None)
    return value if isinstance(value, str) and value else None


def _safe_frontend_redirect_path(path: str | None, default_path: str) -> str:
    if not path:
        return default_path
    if not path.startswith("/") or path.startswith("//"):
        return default_path
    return path


def _frontend_redirect_url(path: str) -> str:
    return urljoin(f"{settings.frontend_url.rstrip('/')}/", path.lstrip("/"))


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append((key, value))
    return urlunsplit(parsed._replace(query=urlencode(query)))


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

class SignupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str
    last_name: str
    email: str
    password: str
    municipality_code: str = Field(pattern=r"^[0-9]{6}$")


def _signup_error_response(exc: Exception) -> tuple[int, str]:
    detail = str(exc).strip() or "Signup failed"
    lowered = detail.lower()
    if "already registered" in lowered or "already exists" in lowered:
        return 400, "Registrazione non riuscita. Verifica i dati inseriti oppure accedi se hai già un account."
    if "password" in lowered:
        return 400, "Password non valida"
    if "email" in lowered:
        return 400, "Email non valida"
    return 400, "Registrazione non riuscita. Riprova più tardi."


def _signup_user_id(response: object) -> str | None:
    user = getattr(response, "user", None)
    user_id = getattr(user, "id", None)
    identities = getattr(user, "identities", None)
    if not identities:
        return None
    return user_id if isinstance(user_id, str) and user_id else None


def _signup_credentials(body: SignupBody) -> dict:
    display_name = f"{body.first_name.strip()} {body.last_name.strip()}".strip()
    return {
        "email": body.email,
        "password": body.password,
        "options": {
            "data": {
                "display_name": display_name,
                "first_name": body.first_name,
                "last_name": body.last_name,
            }
        },
    }


def _validate_signup_municipality(sb: object, municipality_code: str) -> None:
    try:
        get_municipality(sb, municipality_code)
    except MunicipalityNotFoundError as error:
        raise HTTPException(status_code=422, detail="Seleziona un Comune dall'elenco") from error


def _persist_signup_municipality(sb: object, user_id: str, municipality_code: str) -> None:
    sb.table("user_profiles").update({
        "municipality_code": municipality_code,
    }).eq("id", user_id).execute()


def signup_user(body: SignupBody) -> None:
    """Register a user and persist the selected Comune without user coordinates."""
    sb = get_supabase()
    _validate_signup_municipality(sb, body.municipality_code)
    try:
        response = sb.auth.sign_up(_signup_credentials(body))
    except Exception as exc:
        status_code, detail = _signup_error_response(exc)
        logger.exception("Signup failed for %s", body.email)
        raise HTTPException(status_code=status_code, detail=detail) from exc
    user_id = _signup_user_id(response)
    if user_id:
        _persist_signup_municipality(sb, user_id, body.municipality_code)
    else:
        logger.info("Signup returned no new user for %s", body.email)


@router.post("/signup", status_code=201)
async def signup(body: SignupBody) -> dict:
    signup_user(body)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------

class ForgotPasswordBody(BaseModel):
    email: str


def send_password_reset(email: str) -> None:
    """Send a password-reset email via Supabase — never leaks whether email exists."""
    sb = _fresh_supabase_client()
    try:
        redirect_to = f"{settings.backend_url}/auth/callback"
        sb.auth.reset_password_email(email, {"redirect_to": redirect_to})
    except Exception:
        pass


@router.post("/forgot-password", status_code=204, response_model=None)
async def forgot_password(body: ForgotPasswordBody) -> None:
    send_password_reset(body.email)


# ---------------------------------------------------------------------------
# Auth callback (Supabase → backend recovery token)
# ---------------------------------------------------------------------------

@router.get("/callback")
async def auth_callback(
    token_hash: str,
    type: str,
    next: str | None = None,
) -> RedirectResponse:
    """Exchange a Supabase token_hash for a short-lived backend recovery token.

    Only the 'recovery' type is supported.  For other types, redirect to the
    home page without issuing a recovery token.
    """
    if type != "recovery":
        return RedirectResponse(url=settings.frontend_url, status_code=302)

    sb = _fresh_supabase_client()
    try:
        result = sb.auth.verify_otp({"token_hash": token_hash, "type": "recovery"})
        user = result.user
        if not user:
            raise ValueError("No user returned from OTP verification")
    except Exception:
        return RedirectResponse(
            url=f"{settings.frontend_url}/link-scaduto",
            status_code=302,
        )
    recovery_token = create_session_token(
        {
            "sub": user.id,
            "purpose": "password_reset",
            "auth_user_updated_at": _auth_user_updated_at(user),
        },
        lifetime_seconds=_PASSWORD_RESET_TTL_SECONDS,
    )
    redirect_path = _safe_frontend_redirect_path(next, "/reset-password")
    redirect_to = _append_query_param(
        _frontend_redirect_url(redirect_path),
        "token",
        recovery_token,
    )
    return RedirectResponse(
        url=redirect_to,
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------

class ResetPasswordBody(BaseModel):
    recovery_token: str
    password: str


@router.post("/reset-password", status_code=204, response_model=None)
async def reset_password(body: ResetPasswordBody) -> None:
    payload = read_session_token(body.recovery_token)
    if not payload or payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid or expired recovery token")

    user_id: str = payload["sub"]
    sb = _fresh_supabase_client()
    try:
        current_user = sb.auth.admin.get_user_by_id(user_id).user
        if not current_user:
            raise ValueError("Missing user")
        if _auth_user_updated_at(current_user) != payload.get("auth_user_updated_at"):
            raise HTTPException(status_code=400, detail="Invalid or expired recovery token")
        sb.auth.admin.update_user_by_id(user_id, {"password": body.password})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Password reset failed") from exc
