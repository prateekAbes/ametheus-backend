import pytest
from app.core.security import create_access_token

@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401(client):
    # Unauthenticated request to protected endpoint must return 401
    res = await client.get("/api/v1/auth/me")
    assert res.status_code == 401
    assert "Unauthorized" in res.json()["detail"]

@pytest.mark.asyncio
async def test_invalid_token_returns_401(client):
    headers = {"Authorization": "Bearer invalid_garbage_token_xyz"}
    res = await client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 401

@pytest.mark.asyncio
async def test_valid_token_authenticates_user(client):
    # Generate valid HS256 token for user_alpha
    token = create_access_token({"sub": "user_alpha_clerk_id"})
    headers = {"Authorization": f"Bearer {token}"}
    res = await client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "PLAYER"
    assert data["id"] == "user_alpha_clerk_id"

@pytest.mark.asyncio
async def test_clerk_user_sync_endpoint(client):
    payload = {
        "clerk_user_id": "user_synced_clerk_999",
        "email": "synced@ametheus.gg",
        "username": "synced_player",
        "display_name": "Synced Legend",
        "avatar_url": "https://r2.ametheus.gg/avatars/999.png",
    }
    res = await client.post("/api/v1/auth/sync", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["clerk_user_id"] == "user_synced_clerk_999"
    assert data["role"] == "PLAYER"
    assert data["display_name"] == "Synced Legend"

@pytest.mark.asyncio
async def test_super_admin_promote_organizer(client):
    headers_super = {"x-user-id": "usr_super_admin"}
    headers_player = {"x-user-id": "user_synced_clerk_999"}

    # 1. Non-super admin cannot promote
    res_fail = await client.post(
        "/api/v1/admin/users/user_synced_clerk_999/promote-organizer",
        headers=headers_player,
    )
    assert res_fail.status_code == 403

    # 2. Super admin promotes player
    res_ok = await client.post(
        "/api/v1/admin/users/user_synced_clerk_999/promote-organizer",
        headers=headers_super,
    )
    assert res_ok.status_code == 200
    assert "ORGANIZER" in res_ok.json()["message"]

    # 3. Verify user list
    res_users = await client.get("/api/v1/admin/users", headers=headers_super)
    assert res_users.status_code == 200
    users = res_users.json()
    promoted = next(u for u in users if u["id"] == "user_synced_clerk_999")
    assert promoted["role"] == "ORGANIZER"
