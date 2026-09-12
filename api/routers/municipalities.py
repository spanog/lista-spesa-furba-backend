"""Read-only municipality catalog for Comune-only location selection."""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from core.database import get_supabase
from services.municipalities import normalize_municipality_text

router = APIRouter()


class MunicipalitySuggestionResponse(BaseModel):
    code: str
    name: str
    province_name: str
    province_code: str


@router.get("", response_model=list[MunicipalitySuggestionResponse])
def list_municipalities(
    query: str = Query(..., min_length=2, max_length=100),
) -> list[MunicipalitySuggestionResponse]:
    """Return canonical Comune suggestions without coordinates."""
    normalized_query = normalize_municipality_text(query)
    response = (
        get_supabase()
        .table("municipalities")
        .select("code,name,province_name,province_code,normalized_name")
        .ilike("normalized_name", f"%{normalized_query}%")
        .order("name")
        .limit(100)
        .execute()
    )
    return [
        MunicipalitySuggestionResponse(**row)
        for row in _rank_suggestions(response.data or [], normalized_query)
    ]


def _rank_suggestions(rows: list[dict], query: str) -> list[dict]:
    """Prioritize exact and prefix Comune names before partial matches."""
    return sorted(rows, key=lambda row: _suggestion_sort_key(row, query))[:10]


def _suggestion_sort_key(row: dict, query: str) -> tuple[int, str]:
    name = row.get("normalized_name", "")
    rank = 0 if name == query else 1 if name.startswith(query) else 2
    return rank, row.get("name", "")
