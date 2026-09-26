import pytest
from datetime import datetime, timezone
from tests.conftest import TestSessionLocal, make_auth_headers
from app.models.user import User
from app.models.tournament import Tournament, TournamentStage, StageRound
from app.models.match import Match
from app.models.team import Team
from app.models.payment import WalletLedger

@pytest.mark.asyncio
async def test_dynamic_stages_rounds_and_advancement(client):
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u_admin = User(
            id="usr_super_admin",
            clerk_user_id="usr_super_admin",
            email="admin@ametheus.gg",
            username="admin",
            role="SUPER_ADMIN",
        )
        t = Tournament(
            id="tourn_dynamic_01",
            slug="tourn-dynamic-01",
            title="Ametheus Championship",
            name="Ametheus Championship",
            game_id="bgmi",
            game_name="Battlegrounds Mobile India",
            prize_pool_inr=50000,
            entry_fee_inr=100,
            status="REGISTRATION_CLOSED",
            starts_at="Tomorrow, 6:00 PM IST",
            roster_lock_time="5:30 PM IST",
            banner_url="http://banner",
            tournament_type="CUSTOM",
            match_format="BO1",
            series_format="BO1",
            live_stream_url="https://youtube.com/live/ametheus_main",
            stages=[],
        )
        team1 = Team(id="team_alpha", name="Team Alpha", tag="ALP", captain_id="usr_cap_1", game_id="bgmi")
        team2 = Team(id="team_beta", name="Team Beta", tag="BET", captain_id="usr_cap_2", game_id="bgmi")
        team3 = Team(id="team_gamma", name="Team Gamma", tag="GAM", captain_id="usr_cap_3", game_id="bgmi")
        team4 = Team(id="team_delta", name="Team Delta", tag="DEL", captain_id="usr_cap_4", game_id="bgmi")

        db.add_all([u_admin, t, team1, team2, team3, team4])

    # 1. Create Stage 1 (Knockout)
    s1_res = await client.post(
        "/api/v1/admin/tournaments/tourn_dynamic_01/stages",
        json={"name": "Stage 1 - Qualifiers", "format": "KNOCKOUT", "series_format": "BO1", "advancing_teams_count": 2},
        headers=headers_admin,
    )
    assert s1_res.status_code == 200
    s1_data = s1_res.json()["stage"]
    stage1_id = s1_data["stage_id"]
    assert s1_data["stage_name"] == "Stage 1 - Qualifiers"

    # 2. Create Stage 2 (Round Robin)
    s2_res = await client.post(
        "/api/v1/admin/tournaments/tourn_dynamic_01/stages",
        json={"name": "Stage 2 - Group Battle", "format": "ROUND_ROBIN", "series_format": "BO3", "advancing_teams_count": 1},
        headers=headers_admin,
    )
    assert s2_res.status_code == 200
    stage2_id = s2_res.json()["stage"]["stage_id"]

    # 3. Create Round 1 in Stage 1
    r1_res = await client.post(
        f"/api/v1/admin/tournaments/tourn_dynamic_01/stages/{stage1_id}/rounds",
        json={"name": "Quarterfinals", "series_format": "BO1"},
        headers=headers_admin,
    )
    assert r1_res.status_code == 200
    round1_id = r1_res.json()["round"]["round_id"]

    # 4. Create Round 1 in Stage 2
    r2_res = await client.post(
        f"/api/v1/admin/tournaments/tourn_dynamic_01/stages/{stage2_id}/rounds",
        json={"name": "Group Stage Matches", "series_format": "BO3"},
        headers=headers_admin,
    )
    assert r2_res.status_code == 200
    round2_id = r2_res.json()["round"]["round_id"]

    # 5. Create a Match in Stage 1 Round 1
    m1_res = await client.post(
        f"/api/v1/admin/tournaments/tourn_dynamic_01/stages/{stage1_id}/rounds/{round1_id}/matches",
        json={
            "team_a_id": "team_alpha",
            "team_b_id": "team_beta",
            "team_a_name": "Team Alpha",
            "team_b_name": "Team Beta",
            "series_format": "BO1",
        },
        headers=headers_admin,
    )
    assert m1_res.status_code == 200
    match1_id = m1_res.json()["match"]["id"]
    assert m1_res.json()["match"]["team_a_name"] == "Team Alpha"

    # 6. Create a Destination Match in Stage 2 Round 1 (empty slots)
    m2_res = await client.post(
        f"/api/v1/admin/tournaments/tourn_dynamic_01/stages/{stage2_id}/rounds/{round2_id}/matches",
        json={
            "series_format": "BO3",
        },
        headers=headers_admin,
    )
    assert m2_res.status_code == 200
    match2_id = m2_res.json()["match"]["id"]

    # 7. Test Swap Teams on Match 1
    swap_res = await client.post(
        f"/api/v1/admin/matches/{match1_id}/swap",
        headers=headers_admin,
    )
    assert swap_res.status_code == 200
    assert swap_res.json()["match"]["team_a_name"] == "Team Beta"
    assert swap_res.json()["match"]["team_b_name"] == "Team Alpha"

    # Swap back
    await client.post(f"/api/v1/admin/matches/{match1_id}/swap", headers=headers_admin)

    # 8. Check Advancement Options
    adv_opt_res = await client.get(
        f"/api/v1/admin/matches/{match1_id}/advancement-options",
        headers=headers_admin,
    )
    assert adv_opt_res.status_code == 200
    adv_data = adv_opt_res.json()
    assert len(adv_data["available_destinations"]) >= 2

    # 9. Advance Winner of Match 1 (Team Alpha) into Match 2 Slot Team A
    advance_res = await client.post(
        f"/api/v1/admin/matches/{match1_id}/advance",
        json={
            "winner_team_id": "team_alpha",
            "destination_stage_id": stage2_id,
            "destination_round_id": round2_id,
            "destination_match_id": match2_id,
            "destination_slot": "team_a",
        },
        headers=headers_admin,
    )
    assert advance_res.status_code == 200
    assert advance_res.json()["destination_match"]["team_a_id"] == "team_alpha"

    # 10. Revert Advancement
    revert_res = await client.post(
        f"/api/v1/admin/matches/{match1_id}/revert-advancement",
        headers=headers_admin,
    )
    assert revert_res.status_code == 200
    assert revert_res.json()["success"] is True

    # Check match2 team_a_id is cleared
    check_m2 = await client.get(f"/api/v1/matches/{match2_id}")
    assert check_m2.status_code == 200
    assert check_m2.json()["team_a_id"] is None

    # 11. Re-advance Team Alpha
    await client.post(
        f"/api/v1/admin/matches/{match1_id}/advance",
        json={
            "winner_team_id": "team_alpha",
            "destination_stage_id": stage2_id,
            "destination_round_id": round2_id,
            "destination_match_id": match2_id,
            "destination_slot": "team_a",
        },
        headers=headers_admin,
    )

    # 12. Check Tournament History
    hist_res = await client.get("/api/v1/tournaments/tourn_dynamic_01/history")
    assert hist_res.status_code == 200
    hist = hist_res.json()
    assert hist["tournament_id"] == "tourn_dynamic_01"
    assert len(hist["stages"]) == 2

    # 13. Check Team Journey for Team Alpha
    journey_res = await client.get("/api/v1/tournaments/tourn_dynamic_01/teams/team_alpha/journey")
    assert journey_res.status_code == 200
    j_data = journey_res.json()
    assert len(j_data["journey"]) >= 1

    # 14. Check Permanent Live Stream Update
    stream_res = await client.patch(
        "/api/v1/admin/tournaments/tourn_dynamic_01/stream",
        json={"live_stream_url": "https://youtube.com/live/ametheus_new_stream"},
        headers=headers_admin,
    )
    assert stream_res.status_code == 200
    assert stream_res.json()["live_stream_url"] == "https://youtube.com/live/ametheus_new_stream"


@pytest.mark.asyncio
async def test_wallet_audit_logs(client):
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u_admin = User(
            id="usr_super_admin",
            clerk_user_id="usr_super_admin",
            email="admin@ametheus.gg",
            username="admin",
            role="SUPER_ADMIN",
        )
        u_player = User(
            id="usr_player_99",
            clerk_user_id="usr_player_99",
            email="player99@ametheus.gg",
            username="pro_gamer_99",
            role="PLAYER",
            inr_balance_paise=25000,  # ₹250.00
        )
        txn1 = WalletLedger(
            id="txn_01",
            user_id="usr_player_99",
            txn_type="WALLET_TOPUP",
            amount_paise=25000,
            balance_before_paise=0,
            balance_after_paise=25000,
            description="UPI Topup",
        )
        db.add_all([u_admin, u_player, txn1])

    res = await client.get(
        "/api/v1/admin/wallet-audit-logs?search=pro_gamer_99",
        headers=headers_admin,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["system_aggregate_balance"] >= 250.0
    assert len(data["transactions"]) == 1
    assert data["transactions"][0]["username"] == "pro_gamer_99"
    assert data["transactions"][0]["amount_inr"] == 250.0
