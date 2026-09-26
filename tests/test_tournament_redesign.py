import pytest
from app.models.tournament import Tournament
from app.models.payment import UpiAccount
from app.models.user import User
from tests.conftest import TestSessionLocal, make_auth_headers

@pytest.mark.asyncio
async def test_tournament_direct_upi_and_admin_workflow(client):
    headers_cap1 = make_auth_headers("usr_captain_1")
    headers_cap2 = make_auth_headers("usr_captain_2")
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        # Create users
        u1 = User(id="usr_captain_1", clerk_user_id="usr_captain_1", email="redesign_cap1@test.com", username="captain1", role="PLAYER")
        u2 = User(id="usr_captain_2", clerk_user_id="usr_captain_2", email="redesign_cap2@test.com", username="captain2", role="PLAYER")
        db.add_all([u1, u2])

        # Create active UPI account
        upi_acc = UpiAccount(
            code="UPI_TEST_01",
            upi_id="admin@hdfcbank",
            payee_name="Ametheus Esports Pvt Ltd",
            qr_url="https://r2.ametheus.gg/upi_qr.png",
            status="ACTIVE",
        )
        db.add(upi_acc)

        # Create Tournament
        tourn = Tournament(
            id="tourn-test-01",
            slug="valorant-championship-2026",
            game_id="valorant",
            title="Valorant Championship 2026",
            entry_fee_inr=500,
            prize_pool_inr=50000,
            status="REGISTRATION_OPEN",
            starts_at="Tomorrow",
            roster_lock_time="Tomorrow 6 PM",
            banner_url="https://img.gg/val-tourn",
            max_teams=2,
            min_team_size=1,
            max_team_size=5,
            tournament_type="Single Elimination",
            match_format="Best of 3",
        )
        db.add(tourn)
        await db.commit()

    # Create Team 1 via API
    t1_resp = await client.post(
        "/api/v1/teams",
        json={"game_slug": "valorant", "name": "Team Alpha", "tag": "ALPHA", "captain_handle": "Alpha#01"},
        headers=headers_cap1,
    )
    assert t1_resp.status_code == 201
    team_1_id = t1_resp.json()["id"]

    # Create Team 2 via API
    t2_resp = await client.post(
        "/api/v1/teams",
        json={"game_slug": "valorant", "name": "Team Omega", "tag": "OMEGA", "captain_handle": "Omega#02"},
        headers=headers_cap2,
    )
    assert t2_resp.status_code == 201
    team_2_id = t2_resp.json()["id"]

    # 1. Check registration status before registering
    status_resp = await client.get("/api/v1/tournaments/tourn-test-01/registration-status", headers=headers_cap1)
    assert status_resp.status_code == 200
    assert status_resp.json()["is_registered"] is False

    # 2. Team 1 initiates Direct UPI registration
    upi_init_resp = await client.post(
        "/api/v1/tournaments/tourn-test-01/register-upi",
        json={"team_id": team_1_id},
        headers=headers_cap1,
    )
    assert upi_init_resp.status_code == 200, upi_init_resp.text
    upi_data = upi_init_resp.json()
    assert upi_data["status"] == "INITIATED"
    assert upi_data["amount_inr"] == 500.0
    assert "@" in upi_data["upi_id"]
    payment_id = upi_data["payment_id"]

    # 3. Team 1 submits UTR
    utr_resp = await client.post(
        "/api/v1/tournaments/tourn-test-01/submit-upi-utr",
        json={"payment_id": payment_id, "utr": "UTR999888777666"},
        headers=headers_cap1,
    )
    assert utr_resp.status_code == 200
    assert utr_resp.json()["status"] == "PENDING"

    # 4. Check registration status: now registered with PENDING_VERIFICATION
    status_resp2 = await client.get("/api/v1/tournaments/tourn-test-01/registration-status", headers=headers_cap1)
    assert status_resp2.status_code == 200
    assert status_resp2.json()["is_registered"] is True
    assert status_resp2.json()["registration"]["payment_status"] == "PENDING_VERIFICATION"

    # 5. Admin approves payment
    approve_resp = await client.post(f"/api/v1/admin/payments/{payment_id}/approve", headers=headers_admin)
    assert approve_resp.status_code == 200

    # Verify registration is now PAID
    status_resp3 = await client.get("/api/v1/tournaments/tourn-test-01/registration-status", headers=headers_cap1)
    assert status_resp3.json()["registration"]["payment_status"] == "PAID"

    # 6. Team 2 registers (giving user 2 wallet balance first)
    from app.services.wallet_service import credit_wallet_funds
    from tests.conftest import mock_db
    await credit_wallet_funds(db=mock_db, user_id="usr_captain_2", amount_paise=100000, txn_type="WALLET_TOPUP", reference_id="topup_mock", description="Topup")

    reg2_resp = await client.post(
        "/api/v1/tournaments/tourn-test-01/register",
        json={"team_id": team_2_id},
        headers=headers_cap2,
    )
    assert reg2_resp.status_code == 200, reg2_resp.text
    assert reg2_resp.json()["status"] == "success"

    # Capacity was 2, now 2 teams are registered -> Tournament auto-closed!
    tourn_get = await client.get("/api/v1/tournaments/valorant-championship-2026")
    assert tourn_get.status_code == 200
    assert tourn_get.json()["status"] == "REGISTRATION_CLOSED"

    # Duplicate registration attempt should be rejected
    dup_resp = await client.post(
        "/api/v1/tournaments/tourn-test-01/register",
        json={"team_id": team_2_id},
        headers=headers_cap2,
    )
    assert dup_resp.status_code == 400

    # 7. Check Admin Live Matches Dashboard
    dash_resp = await client.get("/api/v1/admin/matches/live-dashboard", headers=headers_admin)
    assert dash_resp.status_code == 200
    dash_data = dash_resp.json()
    assert len(dash_data["tournaments_ready"]) >= 1
    t_ready = next(t for t in dash_data["tournaments_ready"] if t["id"] == "tourn-test-01")
    assert len(t_ready["teams"]) == 2
    assert t_ready["unpaired_count"] == 2

    # 8. Admin pairs Team 1 vs Team 2
    pair_resp = await client.post(
        "/api/v1/admin/matches/pair",
        json={
            "tournament_id": "tourn-test-01",
            "team_a_id": team_1_id,
            "team_b_id": team_2_id,
            "stage": "Grand Final",
            "match_format": "Best of 3",
            "scheduled_at": "2026-09-28T18:00:00Z",
            "live_link": "https://youtube.com/live/ametheus-test",
        },
        headers=headers_admin,
    )
    assert pair_resp.status_code == 200
    match_id = pair_resp.json()["match_id"]

    # 9. Verify match appears in public matches list with full format and stream link
    matches_list = await client.get("/api/v1/matches?tournament_id=tourn-test-01")
    assert matches_list.status_code == 200
    m_found = matches_list.json()[0]
    assert m_found["id"] == match_id
    assert m_found["format"] == "Best of 3"
    assert m_found["live_link"] == "https://youtube.com/live/ametheus-test"
    assert m_found["team_a"]["name"] == "Team Alpha"
    assert m_found["team_b"]["name"] == "Team Omega"

    # 10. Admin updates status to LIVE
    stat_resp = await client.patch(
        f"/api/v1/admin/matches/{match_id}/status",
        json={"status": "LIVE"},
        headers=headers_admin,
    )
    assert stat_resp.status_code == 200

    # 11. Admin submits score and completes match
    score_resp = await client.post(
        f"/api/v1/admin/matches/{match_id}/score",
        json={
            "score_a": 2,
            "score_b": 1,
            "winner_team_id": team_1_id,
            "complete_match": True,
        },
        headers=headers_admin,
    )
    assert score_resp.status_code == 200

    # Verify match is completed
    m_check = await client.get("/api/v1/matches?tournament_id=tourn-test-01")
    assert m_check.json()[0]["status"] == "COMPLETED"
    assert m_check.json()[0]["winner_team_id"] == team_1_id

@pytest.mark.asyncio
async def test_live_matches_rosters_and_bulk_save(client):
    headers_admin = make_auth_headers("usr_super_admin")

    # 1. Fetch registered rosters
    rosters_resp = await client.get("/api/v1/admin/tournaments/tourn-test-01/registered-rosters", headers=headers_admin)
    assert rosters_resp.status_code == 200
    r_data = rosters_resp.json()
    assert "teams" in r_data
    assert "rosters" in r_data
    assert len(r_data["teams"]) == 2
    assert r_data["teams"][0]["team_name"] in ["Team Alpha", "Team Omega"]
    assert r_data["teams"][0]["captain"]["username"] in ["captain1", "captain2"]

    # 2. Bulk save live match structure
    save_resp = await client.post(
        "/api/v1/admin/tournaments/tourn-test-01/save-live-structure",
        json={
            "match_format": "Single Elimination",
            "series_format": "BO3",
            "matches": [
                {
                    "round_number": 1,
                    "match_number": 1,
                    "stage": "Championship Grand Final",
                    "match_type": "GRAND_FINAL",
                    "series_format": "BO3",
                    "team_a_id": r_data["teams"][0]["team_id"],
                    "team_b_id": r_data["teams"][1]["team_id"],
                    "team_a_name": r_data["teams"][0]["team_name"],
                    "team_b_name": r_data["teams"][1]["team_name"],
                    "scheduled_date": "2026-09-27",
                    "scheduled_time": "10:00 AM",
                    "youtube_live_url": "https://youtube.com/live/championship-final",
                }
            ],
        },
        headers=headers_admin,
    )
    assert save_resp.status_code == 200
    assert save_resp.json()["total_matches"] == 1

    # Verify tournament status remains REGISTRATION_CLOSED
    tourn_check = await client.get("/api/v1/tournaments/valorant-championship-2026")
    assert tourn_check.json()["status"] == "REGISTRATION_CLOSED"
    assert tourn_check.json()["match_format"] == "Single Elimination"
    assert tourn_check.json()["series_format"] == "BO3"

    # Verify matches in DB
    matches_check = await client.get("/api/v1/matches?tournament_id=tourn-test-01")
    assert matches_check.status_code == 200
    m_list = matches_check.json()
    assert len(m_list) == 1
    assert m_list[0]["youtube_live_url"] == "https://youtube.com/live/championship-final"
    assert m_list[0]["match_type"] == "GRAND_FINAL"
    assert m_list[0]["series_format"] == "BO3"
    assert m_list[0]["status"] == "SCHEDULED"

