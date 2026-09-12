from __future__ import annotations

import uuid
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from api.routers.offers import router as offers_router
from core.auth import require_admin_or_manager
from tests.conftest import wait_for_user_bootstrap

app = FastAPI()
app.include_router(offers_router, prefix="/offers")


@pytest.fixture()
def supermarkets(supabase_client, clean_db):
    rows = (
        supabase_client.table("supermarkets")
        .insert(
            [
                {
                    "name": f"Managed Market {uuid.uuid4().hex[:6]}",
                    "slug": f"managed-{uuid.uuid4().hex[:8]}",
                    "municipality_code": "015146",
                },
                {
                    "name": f"Foreign Market {uuid.uuid4().hex[:6]}",
                    "slug": f"foreign-{uuid.uuid4().hex[:8]}",
                    "municipality_code": "058091",
                },
            ]
        )
        .execute()
    ).data
    return {"managed": rows[0], "foreign": rows[1]}


@pytest.fixture()
def manager_profile(supabase_client, supermarkets):
    email = f"manual_offer_mgr_{uuid.uuid4().hex[:8]}@test.local"
    resp = supabase_client.auth.admin.create_user(
        {"email": email, "password": "Test_password_123!", "email_confirm": True}
    )
    user_id = resp.user.id
    wait_for_user_bootstrap(user_id)
    (
        supabase_client.table("user_profiles")
        .update(
            {
                "role": "supermarket_manager",
                "managed_supermarket_id": supermarkets["managed"]["id"],
            }
        )
        .eq("id", user_id)
        .execute()
    )
    yield {
        "id": user_id,
        "role": "supermarket_manager",
        "managed_supermarket_id": supermarkets["managed"]["id"],
    }
    supabase_client.auth.admin.delete_user(user_id)


def _offer_payload(supermarket_id: str) -> dict:
    return {
        "supermarket_id": supermarket_id,
        "name": "Mozzarella test",
        "brand": "Caseificio",
        "category": "alimentari-freschi",
        "price_offer": 1.49,
        "price_original": 1.99,
        "valid_to": "2099-12-31",
    }


class TestManualOffersAuthzIntegration:
    @pytest.fixture(autouse=True)
    def _override_auth(self, manager_profile):
        app.dependency_overrides[require_admin_or_manager] = lambda: manager_profile
        yield
        app.dependency_overrides.clear()

    async def test_manager_can_create_offer_only_for_managed_supermarket(
        self,
        supabase_client,
        supermarkets,
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("api.routers.offers.get_supabase", return_value=supabase_client):
                forbidden_resp = await client.post(
                    "/offers",
                    json=_offer_payload(supermarkets["foreign"]["id"]),
                )
                allowed_resp = await client.post(
                    "/offers",
                    json=_offer_payload(supermarkets["managed"]["id"]),
                )

        assert forbidden_resp.status_code == 403
        assert forbidden_resp.json()["detail"] == (
            "Managers can only create offers for their own supermarket"
        )

        assert allowed_resp.status_code == 201
        assert allowed_resp.json()["supermarket_id"] == supermarkets["managed"]["id"]

        offers = (
            supabase_client.table("offers")
            .select("supermarket_id")
            .eq("name", "Mozzarella test")
            .execute()
        ).data
        assert offers == [{"supermarket_id": supermarkets["managed"]["id"]}]
