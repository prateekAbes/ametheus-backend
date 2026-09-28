import asyncio
import pytest
from tests.conftest import make_auth_headers
from app.models.user import User


@pytest.mark.asyncio
async def test_new_user_sync_and_initialization(client, db_session):
    """1. New user registration/login initializes according to existing schema."""
    clerk_id = "user_new_player_101"
    headers = make_auth_headers(clerk_id)
    payload = {
        "clerk_user_id": clerk_id,
        "email": "newplayer@example.com",
        "username": "new_champion",
        "display_name": "New Champion",
        "avatar_url": "https://img.clerk.com/champion.png",
    }
    res = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == clerk_id
    assert data["clerk_user_id"] == clerk_id
    assert data["email"] == "newplayer@example.com"
    assert data["username"] == "new_champion"
    assert data["role"] == "PLAYER"
    assert data["inr_balance_paise"] == 0
    assert data["inr_balance"] == "0.00"

    # Verify exactly 1 record in database
    count = await db_session.users.count_documents({"_id": clerk_id})
    assert count == 1


@pytest.mark.asyncio
async def test_existing_user_login_and_page_refresh_idempotency(client, db_session):
    """2 & 3. Existing user login and page refresh while logged in are idempotent and return 200."""
    clerk_id = "user_3Js07bDoUSWcN5sw8OY89IkCR84"
    headers = make_auth_headers(clerk_id)
    payload = {
        "clerk_user_id": clerk_id,
        "email": "actual_user@example.com",
        "username": "legend_pilot",
        "display_name": "Legend Pilot",
        "avatar_url": "https://img.clerk.com/pilot.png",
    }

    # Initial sync
    res1 = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res1.status_code == 200

    # Second sync (simulating page refresh or re-login with the exact clerk id from the bug report)
    res2 = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["id"] == clerk_id
    assert res2.json()["email"] == "actual_user@example.com"

    # Third sync
    res3 = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res3.status_code == 200

    # Ensure no duplicate documents created
    count = await db_session.users.count_documents({"_id": clerk_id})
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_rapid_auth_sync_requests(client, db_session):
    """4 & 11 & 12. Multiple rapid simultaneous /auth/sync requests do not throw DuplicateKeyError."""
    clerk_id = "user_concurrent_test_999"
    headers = make_auth_headers(clerk_id)
    payload = {
        "clerk_user_id": clerk_id,
        "email": "concurrent_pilot@example.com",
        "username": "speedy_pilot",
        "display_name": "Speedy Pilot",
        "avatar_url": "https://img.clerk.com/speedy.png",
    }

    # Dispatch 10 simultaneous sync requests concurrently
    tasks = [
        client.post("/api/v1/auth/sync", json=payload, headers=headers)
        for _ in range(10)
    ]
    responses = await asyncio.gather(*tasks)

    # Every single request must succeed with 200 OK — NEVER 500 DuplicateKeyError
    for r in responses:
        assert r.status_code == 200
        assert r.json()["id"] == clerk_id

    # Verify no duplicate _id documents are created
    total_docs = await db_session.users.count_documents({"_id": clerk_id})
    assert total_docs == 1


@pytest.mark.asyncio
async def test_sync_protects_existing_organizer_and_player_roles(client, db_session):
    """5 & 6. Sync does NOT demote an ORGANIZER to PLAYER, and preserves PLAYER."""
    clerk_id = "user_organizer_vip"
    headers = make_auth_headers(clerk_id)

    # Pre-seed user with ORGANIZER role
    await db_session.users.insert_one({
        "_id": clerk_id,
        "clerk_user_id": clerk_id,
        "email": "chief_organizer@ametheus.gg",
        "username": "chief_organizer",
        "username_lower": "chief_organizer",
        "role": "ORGANIZER",
        "inr_balance_paise": 15000,
        "inr_balance": "150.00",
    })

    payload = {
        "clerk_user_id": clerk_id,
        "email": "chief_organizer@ametheus.gg",
        "username": "chief_organizer",
        "display_name": "Chief Organizer",
    }
    res = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    # Role must REMAIN ORGANIZER (not reset to default PLAYER)
    assert data["role"] == "ORGANIZER"

    # Verify in DB
    doc = await db_session.users.find_one({"_id": clerk_id})
    assert doc["role"] == "ORGANIZER"


@pytest.mark.asyncio
async def test_sync_preserves_super_admin_role(client, db_session):
    """7. Existing super_admin account is preserved."""
    clerk_id = "user_custom_super_admin"
    headers = make_auth_headers(clerk_id)

    # Pre-seed user as SUPER_ADMIN
    await db_session.users.insert_one({
        "_id": clerk_id,
        "clerk_user_id": clerk_id,
        "email": "superadmin@ametheus.gg",
        "username": "master_admin",
        "username_lower": "master_admin",
        "role": "SUPER_ADMIN",
        "inr_balance_paise": 0,
        "inr_balance": "0.00",
    })

    payload = {
        "clerk_user_id": clerk_id,
        "email": "superadmin@ametheus.gg",
        "username": "master_admin",
    }
    res = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.json()["role"] == "SUPER_ADMIN"


@pytest.mark.asyncio
async def test_sync_protects_wallet_balance_and_ledger_data(client, db_session):
    """8 & 9. Existing user with wallet balance and ledger records is completely preserved."""
    clerk_id = "user_wallet_holder"
    headers = make_auth_headers(clerk_id)

    # Pre-seed user with ₹500.00 (50,000 paise) and ledger history
    await db_session.users.insert_one({
        "_id": clerk_id,
        "clerk_user_id": clerk_id,
        "email": "wealthy@example.com",
        "username": "wealthy_gamer",
        "username_lower": "wealthy_gamer",
        "role": "PLAYER",
        "inr_balance_paise": 50000,
        "inr_reserved_paise": 10000,
        "inr_balance": "500.00",
    })
    await db_session.wallet_ledger.insert_one({
        "user_id": clerk_id,
        "amount_paise": 50000,
        "transaction_type": "DEPOSIT",
        "status": "COMPLETED",
    })

    payload = {
        "clerk_user_id": clerk_id,
        "email": "wealthy@example.com",
        "username": "wealthy_gamer",
        "display_name": "Updated Name",
    }
    res = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()

    # Balances must NOT be reset to 0
    assert data["inr_balance_paise"] == 50000
    assert data["inr_reserved_paise"] == 10000
    assert data["inr_balance"] == "500.00"

    # Ledger records must remain intact
    ledger_count = await db_session.wallet_ledger.count_documents({"user_id": clerk_id})
    assert ledger_count == 1


@pytest.mark.asyncio
async def test_sync_protects_team_game_and_tournament_handles(client, db_session):
    """10. Existing user with team/game/tournament data preserves competitive handles."""
    clerk_id = "user_esports_pro"
    headers = make_auth_headers(clerk_id)

    # Pre-seed user with custom game IDs
    await db_session.users.insert_one({
        "_id": clerk_id,
        "clerk_user_id": clerk_id,
        "email": "pro@team-liquid.com",
        "username": "liquid_ace",
        "username_lower": "liquid_ace",
        "display_name": "LIQUID ACE",
        "role": "PLAYER",
        "riot_id": "ACE#NA1",
        "steam_id": "STEAM_0:1:12345678",
        "inr_balance_paise": 25000,
        "inr_balance": "250.00",
    })

    payload = {
        "clerk_user_id": clerk_id,
        "email": "pro@team-liquid.com",
        "username": "liquid_ace",
        "display_name": "Clerk Default Name",
        "avatar_url": "https://img.clerk.com/default.png",
    }
    res = await client.post("/api/v1/auth/sync", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()

    # Riot ID, Steam ID, and custom display name must remain untouched
    assert data["riot_id"] == "ACE#NA1"
    assert data["steam_id"] == "STEAM_0:1:12345678"
    assert data["display_name"] == "LIQUID ACE"
