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

@pytest.mark.asyncio
async def test_clerk_sync_preserves_custom_avatar_and_display_name(client):
    uid = "clerk_avatar_preserve_user"
    headers = make_auth_headers(uid)

    # 1. Initial sync with default Clerk avatar
    init_res = await client.post("/api/v1/auth/sync", json={
        "clerk_user_id": uid,
        "email": "avatar_tester@ametheus.gg",
        "username": "avatar_tester",
        "display_name": "Avatar Tester",
        "avatar_url": "https://img.clerk.com/initial_avatar.png",
    }, headers=headers)
    assert init_res.status_code == 200
    assert init_res.json()["avatar_url"] == "https://img.clerk.com/initial_avatar.png"

    # 2. User updates profile with a custom avatar & display name
    update_res = await client.put("/api/v1/auth/profile", json={
        "display_name": "CUSTOM VORTEX ACE",
        "avatar_url": "https://ametheus.gg/api/v1/media/img_custom_123.png",
    }, headers=headers)
    assert update_res.status_code == 200
    assert update_res.json()["display_name"] == "CUSTOM VORTEX ACE"
    assert update_res.json()["avatar_url"] == "https://ametheus.gg/api/v1/media/img_custom_123.png"

    # 3. Clerk sync runs again (e.g. on page refresh) passing old Clerk image
    resync_res = await client.post("/api/v1/auth/sync", json={
        "clerk_user_id": uid,
        "email": "avatar_tester@ametheus.gg",
        "username": "avatar_tester",
        "display_name": "Old Clerk Name",
        "avatar_url": "https://img.clerk.com/initial_avatar.png",
    }, headers=headers)
    assert resync_res.status_code == 200
    resync_data = resync_res.json()
    # Must preserve the custom avatar and custom display name
    assert resync_data["avatar_url"] == "https://ametheus.gg/api/v1/media/img_custom_123.png"
    assert resync_data["display_name"] == "CUSTOM VORTEX ACE"

    # 4. Verify GET /me also returns the custom avatar
    me_res = await client.get("/api/v1/auth/me", headers=headers)
    assert me_res.status_code == 200
    assert me_res.json()["avatar_url"] == "https://ametheus.gg/api/v1/media/img_custom_123.png"

@pytest.mark.asyncio
async def test_multiple_sequential_avatar_uploads_and_persistence(client):
    from bson import ObjectId
    uid = "clerk_multi_avatar_user"
    headers = make_auth_headers(uid)

    # 1. Initial Sync
    init_res = await client.post("/api/v1/auth/sync", json={
        "clerk_user_id": uid,
        "email": "multi_avatar@ametheus.gg",
        "username": "multi_tester",
        "display_name": "Multi Tester",
        "avatar_url": "https://img.clerk.com/default.png",
    }, headers=headers)
    assert init_res.status_code == 200

    # Cycle 1: Upload picture-A
    p1_res = await client.put("/api/v1/auth/profile", json={
        "avatar_url": "/api/v1/media/img_picture_A.png",
    }, headers=headers)
    assert p1_res.status_code == 200
    assert p1_res.json()["avatar_url"] == "/api/v1/media/img_picture_A.png"

    # Refresh simulation 1
    ref1 = await client.post("/api/v1/auth/sync", json={
        "clerk_user_id": uid,
        "email": "multi_avatar@ametheus.gg",
        "username": "multi_tester",
        "display_name": "Multi Tester",
        "avatar_url": "https://img.clerk.com/default.png",
    }, headers=headers)
    assert ref1.status_code == 200
    assert ref1.json()["avatar_url"] == "/api/v1/media/img_picture_A.png"

    # Cycle 2: Upload picture-B
    p2_res = await client.put("/api/v1/auth/profile", json={
        "avatar_url": "/api/v1/media/img_picture_B.png",
    }, headers=headers)
    assert p2_res.status_code == 200
    assert p2_res.json()["avatar_url"] == "/api/v1/media/img_picture_B.png"

    # Refresh simulation 2
    ref2 = await client.get("/api/v1/auth/me", headers=headers)
    assert ref2.status_code == 200
    assert ref2.json()["avatar_url"] == "/api/v1/media/img_picture_B.png"

    # Cycle 3: Upload picture-C
    p3_res = await client.put("/api/v1/auth/profile", json={
        "avatar_url": "/api/v1/media/img_picture_C.png",
    }, headers=headers)
    assert p3_res.status_code == 200
    assert p3_res.json()["avatar_url"] == "/api/v1/media/img_picture_C.png"

    # Refresh simulation 3
    ref3 = await client.post("/api/v1/auth/sync", json={
        "clerk_user_id": uid,
        "email": "multi_avatar@ametheus.gg",
        "username": "multi_tester",
        "display_name": "Multi Tester",
        "avatar_url": "https://img.clerk.com/default.png",
    }, headers=headers)
    assert ref3.status_code == 200
    assert ref3.json()["avatar_url"] == "/api/v1/media/img_picture_C.png"

    # Cycle 4: Change name ONLY (picture-C must remain)
    name_res = await client.put("/api/v1/auth/profile", json={
        "display_name": "RENAMED CONTENDER",
    }, headers=headers)
    assert name_res.status_code == 200
    assert name_res.json()["display_name"] == "RENAMED CONTENDER"
    assert name_res.json()["avatar_url"] == "/api/v1/media/img_picture_C.png"

    # Cycle 5: Change name AND picture together
    both_res = await client.put("/api/v1/auth/profile", json={
        "display_name": "FINAL CHAMPION",
        "avatar_url": "/api/v1/media/img_picture_D.png",
    }, headers=headers)
    assert both_res.status_code == 200
    assert both_res.json()["display_name"] == "FINAL CHAMPION"
    assert both_res.json()["avatar_url"] == "/api/v1/media/img_picture_D.png"

    # Cycle 6: Verify user record with native ObjectId in MongoDB updates without failing
    from app.core.database import get_db
    from app.main import app
    db = await anext(app.dependency_overrides[get_db]())
    oid = ObjectId()
    oid_uid = "clerk_oid_user"
    oid_headers = make_auth_headers(oid_uid)
    await db.users.insert_one({
        "_id": oid,
        "clerk_user_id": oid_uid,
        "email": "oid_user@ametheus.gg",
        "username": "oid_tester",
        "display_name": "OID Tester",
        "avatar_url": "old_avatar.png",
        "role": "PLAYER",
        "inr_balance_paise": 0,
        "inr_balance": "0.00",
    })
    oid_update = await client.put("/api/v1/auth/profile", json={
        "avatar_url": "/api/v1/media/img_oid_new.png",
    }, headers=oid_headers)
    assert oid_update.status_code == 200
    assert oid_update.json()["avatar_url"] == "/api/v1/media/img_oid_new.png"
    # Verify in DB directly by ObjectId
    db_doc = await db.users.find_one({"_id": oid})
    assert db_doc["avatar_url"] == "/api/v1/media/img_oid_new.png"



