"""Municipality lookup helpers backed by the versioned ISTAT catalog."""
from __future__ import annotations

import unicodedata


class MunicipalityNotFoundError(ValueError):
    """Raised when a municipality code or a branch municipality is unknown."""


def normalize_municipality_text(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFD", value or "")
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


def get_municipality(sb, code: str) -> dict:
    response = (
        sb.table("municipalities")
        .select("code,name,province_name,province_code")
        .eq("code", code)
        .maybe_single()
        .execute()
    )
    if not response.data:
        raise MunicipalityNotFoundError(code)
    return response.data
