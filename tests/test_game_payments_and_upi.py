import pytest
from app.models.user import User
from app.models.game import Game
from app.models.team import Team
from tests.conftest import TestSessionLocal, make_auth_headers

@pytest.mark.asyncio
async def test_upi_account_crud_and_rotation(client):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. Admin creates UPI accounts
    acct1_payload = {
        "code": "UPI001",
        "upi_id": "test1@oksbi",
        "payee_name": "Ametheus Gaming 1",
        "display_name": "Primary QR",
        "qr_url": "https://example.com/qr1.png",
        "status": "ACTIVE",
    }
    r = await client.post("/api/v1/admin/upi-accounts", json=acct1_payload, headers=admin_headers)
    assert r.status_code in (200, 201)
    acct1 = r.json()
    assert acct1["code"] == "UPI001"

    acct2_payload = {
        "code": "UPI002",
        "upi_id": "test2@okhdfc",
        "payee_name": "Ametheus Gaming 2",
        "display_name": "Backup QR",
        "qr_url": "https://example.com/qr2.png",
        "status": "ACTIVE",
    }
    r = await client.post("/api/v1/admin/upi-accounts", json=acct2_payload, headers=admin_headers)
    assert r.status_code in (200, 201)

    # 2. List UPI accounts
    r = await client.get("/api/v1/admin/upi-accounts", headers=admin_headers)
    assert r.status_code == 200
    accounts = r.json()
    assert len(accounts) >= 2

    # 3. Create a paid Game and a Team
    game_payload = {
        "slug": "bgmi-championship",
        "name": "Battlegrounds Mobile India",
        "category": "mobile",
        "platform": "MOBILE",
        "tag": "BATTLE_ROYALE",
        "is_team_game": True,
        "min_team_size": 4,
        "max_team_size": 4,
        "entry_fee_inr": 100,
        "prize_pool_inr": 50000,
        "banner_url": "https://example.com/banner.jpg",
        "logo_url": "https://example.com/logo.jpg",
        "description": "BGMI Squads League",
        "server_region": "AWS Mumbai",
        "anti_cheat": "BattleEye",
        "match_protocol": "Custom Room",
    }
    r = await client.post("/api/v1/games", json=game_payload, headers=admin_headers)
    assert r.status_code in (200, 201)

    # Player creates squad
    team_payload = {
        "game_slug": "bgmi-championship",
        "name": "Eagle Warriors",
        "tag": "EGL",
        "captain_handle": "EagleCap",
    }
    r = await client.post("/api/v1/teams", json=team_payload, headers=player_headers)
    assert r.status_code in (200, 201)
    team = r.json()
    assert team["payment_status"] == "UNPAID"

    # 4. Initiate Game Payment with UPI
    pay_init_payload = {
        "game_slug": "bgmi-championship",
        "team_id": team["id"],
        "payment_method": "UPI",
    }
    r = await client.post("/api/v1/payments/game/initiate", json=pay_init_payload, headers=player_headers)
    assert r.status_code == 200
    pay_res = r.json()
    assert pay_res["amount_inr"] == 100
    assert pay_res["payment_account_code"] in ["UPI001", "UPI002"]
    assert pay_res["upi_id"] in ["test1@oksbi", "test2@okhdfc"]
    payment_id = pay_res["payment_id"]

    # 5. Submit UTR
    utr_payload = {
        "utr": "UTR123456789012",
        "screenshot_url": "https://example.com/receipt.jpg",
    }
    r = await client.post(f"/api/v1/payments/game/{payment_id}/submit-utr", json=utr_payload, headers=player_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "PENDING"

    # 6. Admin views payments
    r = await client.get("/api/v1/admin/payments?status_filter=PENDING", headers=admin_headers)
    assert r.status_code == 200
    payments = r.json()
    matching = [p for p in payments if p["id"] == payment_id]
    assert len(matching) == 1

    # 7. Admin approves payment
    r = await client.post(f"/api/v1/admin/payments/{payment_id}/approve", headers=admin_headers)
    assert r.status_code == 200

    # Verify team is now PAID
    r = await client.get(f"/api/v1/teams/{team['id']}", headers=player_headers)
    assert r.status_code == 200
    assert r.json()["payment_status"] == "PAID"

    # 8. Admin creates winning payout
    payout_payload = {
        "user_id": "usr_vortex_lead",
        "game_id": "bgmi-championship",
        "rank_or_placement": "1st Place",
        "amount_inr": 25000,
        "destination": "UPI",
        "upi_id": "winner@oksbi",
    }
    r = await client.post("/api/v1/admin/payouts/create-winning", json=payout_payload, headers=admin_headers)
    assert r.status_code == 200

    # 9. List payouts
    r = await client.get("/api/v1/admin/payouts", headers=admin_headers)
    assert r.status_code == 200
    payouts = r.json()
    assert len(payouts) >= 1
    p_id = payouts[0]["id"]

    # 10. Mark payout complete
    r = await client.post(f"/api/v1/admin/payouts/{p_id}/complete", json={"outgoing_utr": "OUT_UTR_998877"}, headers=admin_headers)
    assert r.status_code == 200

@pytest.mark.asyncio
async def test_multiple_payments_without_utr_do_not_conflict(client):
    player_headers = make_auth_headers("usr_vortex_lead")

    # Initiate first wallet top-up (utr is None)
    r1 = await client.post("/api/v1/payments/wallet/topup/initiate", json={"amount_inr": 100}, headers=player_headers)
    assert r1.status_code == 200
    p1 = r1.json()

    # Initiate second wallet top-up (utr is None) — must NOT fail with DuplicateKeyError
    r2 = await client.post("/api/v1/payments/wallet/topup/initiate", json={"amount_inr": 200}, headers=player_headers)
    assert r2.status_code == 200
    p2 = r2.json()

    assert p1["payment_id"] != p2["payment_id"]

@pytest.mark.asyncio
async def test_legacy_upi_accounts_serialization_without_created_at(client, db_session):
    from bson.objectid import ObjectId
    admin_headers = make_auth_headers("usr_super_admin")
    raw_oid = ObjectId()
    legacy_doc = {
        "_id": raw_oid,
        "code": "LEGACY01",
        "upi_id": "legacy@bank",
        "payee_name": "Legacy Merchant",
    }
    await db_session.upi_accounts.insert_one(legacy_doc)

    r = await client.get("/api/v1/admin/upi-accounts", headers=admin_headers)
    assert r.status_code == 200
    accounts = r.json()
    legacy = next((a for a in accounts if a.get("code") == "LEGACY01"), None)
    assert legacy is not None
    assert legacy["id"] == str(raw_oid)
    assert legacy["upi_id"] == "legacy@bank"
    assert legacy["created_at"] is None
    assert legacy["payment_count"] == 0
    assert legacy["status"] == "ACTIVE"
