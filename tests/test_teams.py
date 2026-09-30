import pytest
from tests.conftest import make_auth_headers, TestSessionLocal
from app.models.user import User
from app.models.game import Game

@pytest.mark.asyncio
async def test_team_create_and_join_flow(client):
    headers_captain = make_auth_headers("usr_test_captain")
    headers_member = make_auth_headers("usr_test_teammate")

    # Ensure users exist in DB
    async with TestSessionLocal() as db:
        c = User(id="usr_test_captain", clerk_user_id="usr_test_captain", email="c@test.com", username="test_captain", role="PLAYER")
        m = User(id="usr_test_teammate", clerk_user_id="usr_test_teammate", email="m@test.com", username="test_teammate", role="PLAYER")
        db.add_all([c, m])
        await db.commit()

    # Create Team
    payload = {
        "game_slug": "valorant",
        "name": "Vortex Elite",
        "tag": "VTX",
        "captain_handle": "Vortex#123",
    }

    resp = await client.post("/api/v1/teams", json=payload, headers=headers_captain)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "Vortex Elite"
    assert data["tag"] == "VTX"
    assert len(data["invite_code"]) == 6  # 6-character unique code rule
    assert data["invite_code"].isupper()

    invite_code = data["invite_code"]

    # 2. Join Team via 6-character code (Rule 3.3: ₹0 join fee)
    join_payload = {
        "invite_code": invite_code,
        "player_handle": "Teammate#999",
    }
    join_resp = await client.post("/api/v1/teams/join", json=join_payload, headers=headers_member)
    assert join_resp.status_code == 200, join_resp.text
    join_data = join_resp.json()
    assert len(join_data["members"]) == 2

    # 3. Lookup team by code
    lookup_resp = await client.get(f"/api/v1/teams/code/{invite_code.lower()}")
    assert lookup_resp.status_code == 200
    assert lookup_resp.json()["name"] == "Vortex Elite"

    # 4. Get my teams
    my_teams_resp = await client.get("/api/v1/teams/my-teams", headers=headers_captain)
    assert my_teams_resp.status_code == 200
    my_teams = my_teams_resp.json()
    assert len(my_teams) >= 1
    assert any(t["invite_code"] == invite_code for t in my_teams)

    my_teams_member_resp = await client.get("/api/v1/teams/my-teams", headers=headers_member)
    assert my_teams_member_resp.status_code == 200
    assert any(t["invite_code"] == invite_code for t in my_teams_member_resp.json())

@pytest.mark.asyncio
async def test_team_leave_kick_and_disband(client):
    headers_captain = make_auth_headers("usr_test_captain_disband")
    headers_member = make_auth_headers("usr_test_teammate_disband")

    # 1. Create a fresh team
    payload = {
        "game_slug": "valorant",
        "name": "Shadow Squad",
        "tag": "SHD",
        "captain_handle": "ShadowLead#001",
    }
    create_res = await client.post("/api/v1/teams", json=payload, headers=headers_captain)
    assert create_res.status_code == 201
    team_data = create_res.json()
    team_id = team_data["id"]
    invite_code = team_data["invite_code"]

    # 2. Member joins
    join_res = await client.post("/api/v1/teams/join", json={"invite_code": invite_code, "player_handle": "ShadowOperative#002"}, headers=headers_member)
    assert join_res.status_code == 200
    assert len(join_res.json()["members"]) == 2

    # 3. Captain tries to leave -> 400
    cap_leave_res = await client.post(f"/api/v1/teams/{team_id}/leave", headers=headers_captain)
    assert cap_leave_res.status_code == 400

    # 4. Member leaves voluntarily -> 200
    mem_leave_res = await client.post(f"/api/v1/teams/{team_id}/leave", headers=headers_member)
    assert mem_leave_res.status_code == 200

    # Check member is gone
    t_res = await client.get(f"/api/v1/teams/{team_id}")
    assert len(t_res.json()["members"]) == 1

    # 5. Member rejoins
    await client.post("/api/v1/teams/join", json={"invite_code": invite_code, "player_handle": "ShadowOperative#002"}, headers=headers_member)

    # 6. Unauthorized user tries to kick -> 403
    unauth_kick = await client.post(f"/api/v1/teams/{team_id}/kick/usr_test_captain", headers=headers_member)
    assert unauth_kick.status_code in [400, 403]

    # 7. Captain kicks member -> 200
    kick_res = await client.post(f"/api/v1/teams/{team_id}/kick/usr_test_teammate", headers=headers_captain)
    assert kick_res.status_code == 200

    # 8. Member tries to disband team -> 403
    unauth_disband = await client.delete(f"/api/v1/teams/{team_id}", headers=headers_member)
    assert unauth_disband.status_code == 403

    # 9. Captain disbands team -> 200
    disband_res = await client.delete(f"/api/v1/teams/{team_id}", headers=headers_captain)
    assert disband_res.status_code == 200

    # 10. Verify team is deleted -> 404
    t_after_res = await client.get(f"/api/v1/teams/{team_id}")
    assert t_after_res.status_code == 404

@pytest.mark.asyncio
async def test_solo_game_rejects_team_creation_and_capacity_enforced(client):
    headers_captain = make_auth_headers("usr_cap_solo")
    headers_p2 = make_auth_headers("usr_p2")
    headers_p3 = make_auth_headers("usr_p3")

    async with TestSessionLocal() as db:
        u1 = User(id="usr_cap_solo", clerk_user_id="usr_cap_solo", email="s1@test.com", username="solo_cap", role="PLAYER")
        u2 = User(id="usr_p2", clerk_user_id="usr_p2", email="s2@test.com", username="player_two", role="PLAYER")
        u3 = User(id="usr_p3", clerk_user_id="usr_p3", email="s3@test.com", username="player_three", role="PLAYER")
        g_chess = Game(
            slug="chess-solo-test",
            name="Chess 1v1",
            category="pc",
            platform="PC",
            tag="1V1",
            is_team_game=False,
            min_team_size=1,
            max_team_size=1,
            banner_url="http://banner",
            logo_url="http://logo",
            description="desc",
        )
        g_duo = Game(
            slug="duo-battle-test",
            name="Duo Battle",
            category="pc",
            platform="PC",
            tag="2V2",
            is_team_game=True,
            min_team_size=2,
            max_team_size=2,
            banner_url="http://banner",
            logo_url="http://logo",
            description="desc",
        )
        db.add_all([u1, u2, u3, g_chess, g_duo])
        await db.commit()

    # 1. Attempt team creation for solo game -> 400
    chess_team_res = await client.post(
        "/api/v1/teams",
        json={"game_slug": "chess-solo-test", "name": "Chess Clan", "tag": "CHK", "captain_handle": "Grandmaster"},
        headers=headers_captain,
    )
    assert chess_team_res.status_code == 400
    assert "solo" in chess_team_res.json()["detail"].lower()

    # 2. Team creation for duo game (max 2 members) -> 201
    duo_team_res = await client.post(
        "/api/v1/teams",
        json={"game_slug": "duo-battle-test", "name": "Duo Stars", "tag": "DST", "captain_handle": "Lead#1"},
        headers=headers_captain,
    )
    assert duo_team_res.status_code == 201
    code = duo_team_res.json()["invite_code"]

    # 3. 2nd member joins -> 200 (team now full 2/2)
    join2_res = await client.post("/api/v1/teams/join", json={"invite_code": code, "player_handle": "Mate#2"}, headers=headers_p2)
    assert join2_res.status_code == 200
    assert len(join2_res.json()["members"]) == 2

    # 4. 3rd member attempts to join full team -> 400 "Team roster is already full"
    join3_res = await client.post("/api/v1/teams/join", json={"invite_code": code, "player_handle": "Extra#3"}, headers=headers_p3)
    assert join3_res.status_code == 400
    assert "full" in join3_res.json()["detail"].lower()

@pytest.mark.asyncio
async def test_one_team_per_game_enforcement(client):
    headers_user = make_auth_headers("usr_unique_per_game")

    # 1. Create Team A for Valorant -> 201
    res1 = await client.post(
        "/api/v1/teams",
        json={"game_slug": "valorant", "name": "Team One", "tag": "ONE", "captain_handle": "Captain#1"},
        headers=headers_user,
    )
    assert res1.status_code == 201

    # 2. Attempt to create Team B for Valorant -> 400 (Already belongs to a team for this game)
    res2 = await client.post(
        "/api/v1/teams",
        json={"game_slug": "valorant", "name": "Team Two", "tag": "TWO", "captain_handle": "Captain#1"},
        headers=headers_user,
    )
    assert res2.status_code == 400
    assert "already belong" in res2.json()["detail"].lower()

    # 3. Create Team for a different game (mini-militia) -> 201
    res3 = await client.post(
        "/api/v1/teams",
        json={"game_slug": "mini-militia", "name": "Militia Team", "tag": "MIL", "captain_handle": "Captain#1"},
        headers=headers_user,
    )
    assert res3.status_code == 201

@pytest.mark.asyncio
async def test_coming_soon_game_rejects_team_ops(client):
    headers = make_auth_headers("usr_cs_test")

    async with TestSessionLocal() as db:
        u = User(id="usr_cs_test", clerk_user_id="usr_cs_test", email="cs@test.com", username="cs_player", role="PLAYER")
        g_cs = Game(
            slug="cyberpunk-arena-cs",
            name="Cyberpunk Arena",
            category="pc",
            platform="PC",
            tag="5V5",
            is_team_game=True,
            status="COMING_SOON",
            banner_url="http://banner",
            logo_url="http://logo",
            description="Coming soon title",
        )
        db.add_all([u, g_cs])
        await db.commit()

    # Attempt to create team for COMING_SOON game -> 400
    res = await client.post(
        "/api/v1/teams",
        json={"game_slug": "cyberpunk-arena-cs", "name": "Cyber Team", "tag": "CYB", "captain_handle": "CyberLead"},
        headers=headers,
    )
    assert res.status_code == 400
    assert "coming soon" in res.json()["detail"].lower()

@pytest.mark.asyncio
async def test_team_disband_refunds_all_eligible_payers(client):
    from conftest import mock_db
    from app.models.payment import GamePayment, WalletLedger
    from app.models.tournament import TournamentRegistration, Tournament

    cap_id = "usr_disband_cap"
    mem_a_id = "usr_disband_mem_a"
    headers_cap = make_auth_headers(cap_id)

    async with TestSessionLocal() as db:
        u_cap = User(id=cap_id, clerk_user_id=cap_id, email="cap_d@test.com", username="cap_d", role="PLAYER", inr_balance_paise=10000, inr_balance="100.00")
        u_a = User(id=mem_a_id, clerk_user_id=mem_a_id, email="a_d@test.com", username="mem_a", role="PLAYER", inr_balance_paise=5000, inr_balance="50.00")
        db.add_all([u_cap, u_a])
        await db.commit()

    # 1. Create team
    create_res = await client.post(
        "/api/v1/teams",
        json={"game_slug": "valorant", "name": "Disband Test Team", "tag": "DTT", "captain_handle": "CapHandle"},
        headers=headers_cap,
    )
    assert create_res.status_code == 201
    team_data = create_res.json()
    team_id = team_data["id"]

    # 2. Add an approved UPI payment made by Member A (₹60 = 6000 paise)
    upi_pay = GamePayment(
        id="pay_upi_disband_123",
        payment_type="TOURNAMENT_ENTRY",
        user_id=mem_a_id,
        tournament_id="tourn_test_1",
        team_id=team_id,
        amount_paise=6000,
        amount_inr=60.0,
        payment_method="UPI",
        status="APPROVED",
        utr="UTRDISBAND123",
    )
    await mock_db.game_payments.insert_one(upi_pay.to_mongo())

    # 3. Add a wallet-paid tournament registration made by Captain (₹50 = 5000 paise)
    reg_wallet = TournamentRegistration(
        id="reg_wallet_disband_456",
        tournament_id="tourn_test_1",
        team_id=team_id,
        player_id=cap_id,
        payment_method="WALLET",
        payment_status="PAID",
        order_id="ord_wallet_456",
        wallet_txn_id="txn_wallet_456",
    )
    await mock_db.tournament_registrations.insert_one(reg_wallet.to_mongo())

    # Debit ledger for captain's entry fee
    debit_ledger = WalletLedger(
        id="ledger_debit_456",
        user_id=cap_id,
        txn_type="TOURNAMENT_ENTRY",
        amount_paise=-5000,
        balance_before_paise=15000,
        balance_after_paise=10000,
        reference_id="reg_wallet_disband_456",
        description="Tournament entry fee debit",
    )
    await mock_db.wallet_ledger.insert_one(debit_ledger.to_mongo())

    # 4. Captain disbands team
    disband_res = await client.delete(f"/api/v1/teams/{team_id}", headers=headers_cap)
    assert disband_res.status_code == 200
    disband_data = disband_res.json()
    assert disband_data["status"] == "success"
    assert disband_data["refunded_payments_count"] == 2
    assert disband_data["total_refunded_inr"] == 110.0  # 60 + 50

    # 5. Verify Team is deleted
    team_in_db = await mock_db.teams.find_one({"_id": team_id})
    assert team_in_db is None

    # 6. Verify Registration is cancelled
    reg_in_db = await mock_db.tournament_registrations.find_one({"$or": [{"_id": "reg_wallet_disband_456"}, {"id": "reg_wallet_disband_456"}]})
    assert reg_in_db["payment_status"] == "CANCELLED"
    assert reg_in_db["status"] == "CANCELLED"

    # 7. Verify Member A received ₹60 into Ametheus Wallet (NOT UPI)
    user_a_updated = await mock_db.users.find_one({"$or": [{"_id": mem_a_id}, {"id": mem_a_id}]})
    assert user_a_updated["inr_balance_paise"] == 5000 + 6000  # 11000 paise = ₹110

    # 8. Verify Captain received ₹50 into Ametheus Wallet
    user_cap_updated = await mock_db.users.find_one({"$or": [{"_id": cap_id}, {"id": cap_id}]})
    assert user_cap_updated["inr_balance_paise"] == 10000 + 5000  # 15000 paise = ₹150

    # 9. Verify refund ledgers exist with txn_type="TEAM_DISBAND_REFUND"
    refund_ledgers = await mock_db.wallet_ledger.find({"txn_type": "TEAM_DISBAND_REFUND"}).to_list(10)
    assert len(refund_ledgers) == 2
    mem_a_refund = next(l for l in refund_ledgers if l["user_id"] == mem_a_id)
    assert mem_a_refund["amount_paise"] == 6000
    assert "refund_pay_pay_upi_disband_123" in mem_a_refund["reference_id"]
    cap_refund = next(l for l in refund_ledgers if l["user_id"] == cap_id)
    assert cap_refund["amount_paise"] == 5000
    assert "refund_reg_reg_wallet_disband_456" in cap_refund["reference_id"]

    # 10. Verify game payment record marked as refunded
    pay_in_db = await mock_db.game_payments.find_one({"_id": "pay_upi_disband_123"})
    assert pay_in_db["is_refunded"] is True
    assert pay_in_db["refund_destination"] == "PLAYER_WALLET"

