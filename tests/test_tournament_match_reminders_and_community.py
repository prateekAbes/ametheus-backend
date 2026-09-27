import pytest
from tests.conftest import make_auth_headers, TestSessionLocal, mock_db
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.tournament import Tournament, TournamentRegistration
from app.models.match import Match

@pytest.mark.asyncio
async def test_universal_game_id_and_registration_closure(client):
    headers_player = make_auth_headers("usr_player_univ")
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u = User(
            id="usr_player_univ",
            clerk_user_id="usr_player_univ",
            email="univ@test.com",
            username="player_univ",
            role="PLAYER",
            inr_balance_paise=50000,
        )
        db.add(u)

        g = Game(
            slug="bgmi-test",
            name="BGMI",
            category="mobile",
            platform="Mobile",
            tag="Battle Royale",
            banner_url="http://banner",
            logo_url="http://logo",
            description="desc",
            max_team_size=4,
        )
        db.add(g)
        await db.commit()
        await db.refresh(g)

        t = Tournament(
            slug="bgmi-univ-cup",
            game_id=g.id,
            title="BGMI UNIVERSAL CUP",
            prize_pool_inr=10000,
            entry_fee_inr=0,  # Free
            status="REGISTRATION_OPEN",
            starts_at="Tomorrow, 8:00 PM IST",
            roster_lock_time="7:30 PM IST",
            server_region="India",
            banner_url="http://banner",
            description="BGMI Cup",
            min_team_size=1,
            max_team_size=4,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)

        team = Team(
            game_id=g.id,
            captain_id="usr_player_univ",
            name="Phoenix Squad",
            tag="PHX",
            invite_code="PHX123",
            max_members=4,
            members=[
                TeamMember(user_id="usr_player_univ", username="player_univ", role="CAPTAIN", game_handle="Phoenix#007")
            ],
        )
        db.add(team)
        await db.commit()
        await db.refresh(team)
        t_id = t.id
        team_id = team.id

    # 1. Register with universal Game ID
    reg_payload = {
        "team_id": team_id,
        "game_id": "5123456789",
        "in_game_id": "5123456789",
    }
    reg_resp = await client.post(f"/api/v1/tournaments/{t_id}/register", json=reg_payload, headers=headers_player)
    assert reg_resp.status_code == 200, reg_resp.text
    assert reg_resp.json()["status"] == "success"

    # Verify registration in DB has game_id and in_game_id
    reg = await mock_db.tournament_registrations.find_one({"tournament_id": t_id, "team_id": team_id})
    assert reg is not None
    assert reg.get("game_id") == "5123456789"
    assert reg.get("in_game_id") == "5123456789"

    # 2. Close registration via admin
    close_resp = await client.post(f"/api/v1/admin/tournaments/{t_id}/close-registration", headers=headers_admin)
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "success"

    # 3. Attempting to register after closure returns 400
    reg_after_close = await client.post(f"/api/v1/tournaments/{t_id}/register", json=reg_payload, headers=headers_player)
    assert reg_after_close.status_code == 400
    assert "closed" in reg_after_close.json()["detail"].lower()


@pytest.mark.asyncio
async def test_flexible_rounds_and_community_access(client):
    headers_player = make_auth_headers("usr_player_comm")
    headers_other = make_auth_headers("usr_player_other")
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u1 = User(id="usr_player_comm", clerk_user_id="usr_player_comm", email="comm@test.com", username="comm_player", role="PLAYER")
        u2 = User(id="usr_player_other", clerk_user_id="usr_player_other", email="other@test.com", username="other_player", role="PLAYER")
        db.add(u1)
        db.add(u2)

        t = Tournament(
            game_id="game-comm-mock",
            slug="round-test-tourn",
            title="ROUND TEST TOURNAMENT",
            prize_pool_inr=5000,
            entry_fee_inr=0,
            status="REGISTRATION_OPEN",
            starts_at="Tomorrow",
            roster_lock_time="Soon",
            server_region="India",
            banner_url="http://banner",
            description="Testing rounds & community",
            community_platform="DISCORD",
            community_link="https://discord.gg/secret-invite",
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        t_id = t.id

        # Register u1 as a confirmed participant
        team1 = Team(game_id="game-comm-mock", captain_id="usr_player_comm", name="Comm Team", tag="CT", invite_code="CT1", max_members=1)
        db.add(team1)
        await db.commit()
        await db.refresh(team1)

        reg = TournamentRegistration(
            tournament_id=t_id,
            team_id=team1.id,
            player_id="usr_player_comm",
            payment_status="PAID",
        )
        db.add(reg)
        await db.commit()
        await mock_db.tournaments.update_one(
            {"$or": [{"_id": t_id}, {"id": t_id}]},
            {"$push": {"registered_rosters": reg.model_dump()}}
        )

    # 1. Admin adds custom round
    add_round_resp = await client.post(
        f"/api/v1/admin/tournaments/{t_id}/rounds",
        json={"round_name": "Round 2"},
        headers=headers_admin,
    )
    assert add_round_resp.status_code == 200
    assert "Round 2" in add_round_resp.json()["custom_rounds"]

    # 2. Admin updates community to WhatsApp
    up_comm_resp = await client.patch(
        f"/api/v1/admin/tournaments/{t_id}/community",
        json={"community_platform": "WHATSAPP", "community_link": "https://chat.whatsapp.com/test-group"},
        headers=headers_admin,
    )
    assert up_comm_resp.status_code == 200
    assert up_comm_resp.json()["community_platform"] == "WHATSAPP"

    # 3. Other user (not confirmed participant) tries to view community link -> 403 Forbidden
    other_comm_resp = await client.get(f"/api/v1/tournaments/{t_id}/community", headers=headers_other)
    assert other_comm_resp.status_code == 403

    # 4. Confirmed participant views community link -> 200 OK with private link
    player_comm_resp = await client.get(f"/api/v1/tournaments/{t_id}/community", headers=headers_player)
    assert player_comm_resp.status_code == 200
    data = player_comm_resp.json()
    assert data["community_platform"] == "WHATSAPP"
    assert data["community_link"] == "https://chat.whatsapp.com/test-group"


@pytest.mark.asyncio
async def test_match_join_info_reminders_and_422_scoring_fix(client):
    headers_p1 = make_auth_headers("usr_p1_match")
    headers_outsider = make_auth_headers("usr_outsider")
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u1 = User(id="usr_p1_match", clerk_user_id="usr_p1_match", email="p1@test.com", username="p1_match", role="PLAYER")
        u2 = User(id="usr_outsider", clerk_user_id="usr_outsider", email="out@test.com", username="outsider", role="PLAYER")
        db.add(u1)
        db.add(u2)

        team_desert = Team(
            id="team_desert",
            game_id="game-mock-1",
            captain_id="usr_p1_match",
            name="Desert",
            tag="DST",
            invite_code="DST1",
            max_members=5,
        )
        db.add(team_desert)

        m = Match(
            tournament_id="t-scoring-fix",
            stage="Round 1",
            round_name="Round 1",
            round_number=1,
            match_number=1,
            format="BO1",
            scheduled_time="Today, 9:00 PM IST",
            status="SCHEDULED",
            team_a_name="Desert",
            team_b_name="Storm",
            team_a_placeholder="Desert",
            team_b_placeholder="Storm",
            join_info_enabled=True,
            join_info={"join_code": "DESERT-77", "password": "supersecretpassword"},
            match_message="Server: Mumbai, strict punctuality",
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        m_id = m.id

        # Register p1 in the tournament as team "Desert"
        reg = TournamentRegistration(
            tournament_id="t-scoring-fix",
            team_id="team_desert",
            player_id="usr_p1_match",
            payment_status="PAID",
        )
        db.add(reg)
        await db.commit()

    # 1. Update Join Info credentials via Admin
    up_join_resp = await client.patch(
        f"/api/v1/admin/matches/{m_id}/join-info",
        json={
            "join_info_enabled": True,
            "join_info": {"join_code": "CODE_999", "lobby_id": "LOBBY_42", "password": "pass"},
            "match_message": "Be in lobby 10m early",
        },
        headers=headers_admin,
    )
    assert up_join_resp.status_code == 200
    assert up_join_resp.json()["join_info_enabled"] is True

    # 2. Public / outsider access to match join-info returns 403 Forbidden
    out_resp = await client.get(f"/api/v1/matches/{m_id}/join-info", headers=headers_outsider)
    assert out_resp.status_code == 403

    # 3. Confirmed participant gets join credentials
    p1_resp = await client.get(f"/api/v1/matches/{m_id}/join-info", headers=headers_p1)
    assert p1_resp.status_code == 200
    join_data = p1_resp.json()
    assert join_data["join_info_enabled"] is True
    assert join_data["join_info"]["join_code"] == "CODE_999"
    assert join_data["join_info"]["password"] == "pass"
    assert join_data["match_message"] == "Be in lobby 10m early"

    # 4. Trigger countdown reminder via Admin
    rem_resp = await client.post(
        f"/api/v1/admin/matches/{m_id}/send-reminder",
        json={"reminder_type": "15_MINUTES"},
        headers=headers_admin,
    )
    assert rem_resp.status_code == 200
    assert rem_resp.json()["status"] == "success"

    # Deduplicated reminder: second trigger skips sending
    rem_resp2 = await client.post(
        f"/api/v1/admin/matches/{m_id}/send-reminder",
        json={"reminder_type": "15_MINUTES"},
        headers=headers_admin,
    )
    assert rem_resp2.status_code == 200
    assert rem_resp2.json()["skipped"] is True

    # 5. Complete Match 422 Fix: Payload with { team1_score, team2_score, winner_id } and non-ObjectId winner "Desert"
    complete_payload = {
        "team1_score": 13,
        "team2_score": 9,
        "winner_id": "Desert",
    }
    comp_resp = await client.post(
        f"/api/v1/admin/matches/{m_id}/complete",
        json=complete_payload,
        headers=headers_admin,
    )
    assert comp_resp.status_code == 200, comp_resp.text
    res_json = comp_resp.json()
    assert res_json["status"] == "success"
    assert res_json["winner_id"] == "Desert"
    assert res_json["score_a"] == 13
    assert res_json["score_b"] == 9
