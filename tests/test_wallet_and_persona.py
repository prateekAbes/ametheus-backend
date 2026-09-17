import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import settings

@pytest.mark.asyncio
async def test_persona_pool_public_get():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get(f"{settings.API_V1_STR}/admin/persona-pool")
        assert res.status_code == 200
        data = res.json()
        assert "names" in data
        assert "avatars" in data
        assert len(data["names"]) > 0
        assert len(data["avatars"]) > 0

@pytest.mark.asyncio
async def test_wallet_endpoints_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Without auth header, should require authentication
        res = await ac.post(
            f"{settings.API_V1_STR}/wallet/withdraw",
            json={"amount": 500, "upi_id": "vortex@oksbi"}
        )
        assert res.status_code in [401, 403]
