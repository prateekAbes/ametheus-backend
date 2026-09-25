import pytest
from tests.conftest import make_auth_headers

@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401(client):
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
    headers = make_auth_headers("usr_vortex_lead")
    res = await client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "PLAYER"
    assert data["id"] == "usr_vortex_lead"

@pytest.mark.asyncio
async def test_clerk_user_sync_endpoint(client):
    payload = {
        "clerk_user_id": "user_synced_clerk_999",
        "email": "synced@ametheus.gg",
        "username": "synced_player",
        "display_name": "Synced Legend",
        "avatar_url": "https://r2.ametheus.gg/avatars/999.png",
    }
    headers = make_auth_headers("user_synced_clerk_999")
    res = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["clerk_user_id"] == "user_synced_clerk_999"
    assert data["role"] == "PLAYER"
    assert data["display_name"] == "Synced Legend"

@pytest.mark.asyncio
async def test_super_admin_promote_organizer(client):
    headers_super = make_auth_headers("usr_super_admin")
    headers_player = make_auth_headers("user_synced_clerk_999")

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

@pytest.mark.asyncio
async def test_update_user_profile(client):
    headers = make_auth_headers("usr_vortex_lead")
    payload = {
        "display_name": "VORTEX ACE",
        "riot_id": "VORTEX#ACE",
        "steam_id": "STEAM_0:1:998877",
    }
    res = await client.put("/api/v1/auth/profile", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["display_name"] == "VORTEX ACE"
    assert data["riot_id"] == "VORTEX#ACE"
    assert data["steam_id"] == "STEAM_0:1:998877"

    # Verify GET /me reflects updated profile
    me_res = await client.get("/api/v1/auth/me", headers=headers)
    assert me_res.status_code == 200
    assert me_res.json()["display_name"] == "VORTEX ACE"

@pytest.mark.asyncio
async def test_reserved_admin_username_rejected(client):
    headers = make_auth_headers("usr_vortex_lead")
    for reserved in ["admin", "ADMIN", "Admin", "Administrator", "support", "root"]:
        res = await client.put("/api/v1/auth/profile", json={"username": reserved}, headers=headers)
        assert res.status_code == 400, f"Expected 400 for reserved username '{reserved}', got {res.status_code}"
        assert "reserved" in res.json()["detail"].lower()

@pytest.mark.asyncio
async def test_username_case_insensitive_uniqueness(client):
    # Sync first user
    headers1 = make_auth_headers("clerk_unique_1")
    res1 = await client.post("/api/v1/auth/sync", json={
        "clerk_user_id": "clerk_unique_1",
        "email": "user1@example.com",
        "username": "shadow_ninja",
    }, headers=headers1)
    assert res1.status_code == 200

    # User 2 tries to take "SHADOW_NINJA" (conflict)
    headers2 = make_auth_headers("usr_vortex_lead")
    res2 = await client.put("/api/v1/auth/profile", json={"username": "SHADOW_NINJA"}, headers=headers2)
    assert res2.status_code == 409
    assert "already taken" in res2.json()["detail"].lower()

    # User 2 sets a unique valid username
    res3 = await client.put("/api/v1/auth/profile", json={"username": "unique_gamer_99"}, headers=headers2)
    assert res3.status_code == 200
    assert res3.json()["username"] == "unique_gamer_99"

