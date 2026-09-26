import pytest
from tests.conftest import make_auth_headers, TestSessionLocal
from app.models.user import User
from app.models.game import Game
from app.models.team import Team
from app.models.tournament import Tournament

@pytest.mark.asyncio
async def test_tournament_discovery_and_registration(client):
    headers_captain = make_auth_headers("usr_tourn_captain")

    async with TestSessionLocal() as db:
        u = User(id="usr_tourn_captain", clerk_user_id="usr_tourn_captain", email="tc@test.com", username="tourn_captain", role="PLAYER", inr_balance_paise=10000)
        db.add(u)
        g = Game(
            slug="valorant-tourn-test",
            name="Valorant",
            category="pc",
            platform="PC",
            tag="5V5",
            banner_url="http://banner",
            logo_url="http://logo",
            description="desc",
            max_team_size=5,
        )
        db.add(g)
        await db.commit()
        await db.refresh(g)

        t = Tournament(
            slug="val-test-cup-01",
            game_id=g.id,
            title="VALORANT TEST INVITATIONAL",
            prize_pool_inr=25000,
            entry_fee_inr=50,
            status="REGISTRATION_OPEN",
            starts_at="Tomorrow, 6:00 PM IST",
            roster_lock_time="5:30 PM IST",
            server_region="AWS Mumbai",
            banner_url="http://banner",
            description="Test tournament",
            min_team_size=1,
            max_team_size=5,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)

        from app.models.team import TeamMember
        team = Team(
            game_id=g.id,
            captain_id="usr_tourn_captain",
            name="Alpha Squad",
            tag="ALP",
            invite_code="ALP123",
            max_members=5,
            members=[
                TeamMember(user_id="usr_tourn_captain", username="tourn_captain", role="CAPTAIN", game_handle="cap#123")
            ],
        )
        db.add(team)
        await db.commit()
        await db.refresh(team)

        t_id = t.id
        team_id = team.id

    # 1. List Tournaments
    resp = await client.get("/api/v1/tournaments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1

    # 2. Get Tournament by slug
    slug_resp = await client.get("/api/v1/tournaments/val-test-cup-01")
    assert slug_resp.status_code == 200
    assert slug_resp.json()["title"] == "VALORANT TEST INVITATIONAL"

    # 3. Register Team
    reg_resp = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={"team_id": team_id},
        headers=headers_captain,
    )
    assert reg_resp.status_code == 200
    assert reg_resp.json()["status"] == "success"

@pytest.mark.asyncio
async def test_leaderboard_discovery(client):
    resp = await client.get("/api/v1/leaderboard")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    first = data[0]
    assert "rank" in first
    assert "name" in first
    assert "tag" in first
    assert "rating" in first
    assert "win_rate" in first

    # Category filter test
    cat_resp = await client.get("/api/v1/leaderboard?category=pc")
    assert cat_resp.status_code == 200
    assert isinstance(cat_resp.json(), list)

@pytest.mark.asyncio
async def test_chess_solo_registration_and_squad_rejection(client):
    headers_player = make_auth_headers("usr_chess_gm")
    headers_captain = make_auth_headers("usr_chess_cap")

    async with TestSessionLocal() as db:
        u1 = User(id="usr_chess_gm", clerk_user_id="usr_chess_gm", email="gm@test.com", username="chess_gm", role="PLAYER")
        u2 = User(id="usr_chess_cap", clerk_user_id="usr_chess_cap", email="cap@test.com", username="chess_cap", role="PLAYER")
        fake_team = Team(
            game_id="chess",
            captain_id="usr_chess_cap",
            name="Fake Squad",
            tag="FAK",
            invite_code="FAK123",
            max_members=1,
        )
        db.add_all([u1, u2, fake_team])
        await db.commit()
        team_id = fake_team.id

    t_res = await client.get("/api/v1/tournaments/ametheus-chess-blitz-01")
    assert t_res.status_code == 200
    t_id = t_res.json()["id"]

    # 1. Attempt registering with a team for Chess -> 400 rejection
    squad_res = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={"team_id": team_id},
        headers=headers_captain,
    )
    assert squad_res.status_code == 400
    assert "solo" in squad_res.json()["detail"].lower()

    # 2. Individual solo registration without team_id -> 200 success
    solo_res = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={},
        headers=headers_player,
    )
    assert solo_res.status_code == 200
    assert "solo contender" in solo_res.json()["message"].lower()

    # 3. Duplicate solo registration -> 400
    dup_res = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={},
        headers=headers_player,
    )
    assert dup_res.status_code == 400
    assert "already registered" in dup_res.json()["detail"].lower()

@pytest.mark.asyncio
async def test_dynamic_tournament_team_size_and_live_bracket_matches(client):
    headers_admin = make_auth_headers("usr_tourn_admin")
    headers_cap1 = make_auth_headers("usr_duo_cap1")
    headers_cap2 = make_auth_headers("usr_duo_cap2")

    from app.models.team import TeamMember

    async with TestSessionLocal() as db:
        admin_u = User(id="usr_tourn_admin", clerk_user_id="usr_tourn_admin", email="admin@test.com", username="tourn_admin", role="SUPER_ADMIN")
        u1 = User(id="usr_duo_cap1", clerk_user_id="usr_duo_cap1", email="cap1@test.com", username="duo_cap1", role="PLAYER")
        u2 = User(id="usr_duo_mem1", clerk_user_id="usr_duo_mem1", email="mem1@test.com", username="duo_mem1", role="PLAYER")
        u3 = User(id="usr_duo_cap2", clerk_user_id="usr_duo_cap2", email="cap2@test.com", username="duo_cap2", role="PLAYER")
        u4 = User(id="usr_duo_mem2", clerk_user_id="usr_duo_mem2", email="mem2@test.com", username="duo_mem2", role="PLAYER")

        game = Game(
            slug="shooter-duo-game",
            name="Tactical Shooter",
            category="pc",
            platform="PC",
            tag="5V5",
            banner_url="http://banner",
            logo_url="http://logo",
            description="desc",
            max_team_size=5,
        )
        db.add_all([admin_u, u1, u2, u3, u4, game])
        await db.commit()
        await db.refresh(game)

        # Tournament with required min_team_size = 2
        tourn = Tournament(
            slug="tactical-duo-clash-01",
            game_id=game.id,
            title="Tactical Duo Clash",
            prize_pool_inr=10000,
            entry_fee_inr=0,
            status="REGISTRATION_OPEN",
            starts_at="Today, 8:00 PM IST",
            roster_lock_time="7:30 PM IST",
            server_region="AWS Mumbai",
            banner_url="http://banner",
            description="Duo tournament",
            min_team_size=2,
            max_team_size=3,
        )
        db.add(tourn)
        await db.commit()
        await db.refresh(tourn)

        # Team 1 with only 1 member (should fail requirement of min_team_size=2)
        team_solo = Team(
            game_id=game.id,
            captain_id="usr_duo_cap1",
            name="Solo Attempt",
            tag="SLO",
            invite_code="SLO123",
            max_members=5,
            members=[
                TeamMember(user_id="usr_duo_cap1", username="duo_cap1", role="CAPTAIN", game_handle="cap1#1")
            ],
        )
        # Team 1 valid with 2 members
        team_duo1 = Team(
            game_id=game.id,
            captain_id="usr_duo_cap1",
            name="Duo Team One",
            tag="DT1",
            invite_code="DT1123",
            max_members=5,
            members=[
                TeamMember(user_id="usr_duo_cap1", username="duo_cap1", role="CAPTAIN", game_handle="cap1#1"),
                TeamMember(user_id="usr_duo_mem1", username="duo_mem1", role="MEMBER", game_handle="mem1#1"),
            ],
        )
        # Team 2 valid with 2 members
        team_duo2 = Team(
            game_id=game.id,
            captain_id="usr_duo_cap2",
            name="Duo Team Two",
            tag="DT2",
            invite_code="DT2123",
            max_members=5,
            members=[
                TeamMember(user_id="usr_duo_cap2", username="duo_cap2", role="CAPTAIN", game_handle="cap2#1"),
                TeamMember(user_id="usr_duo_mem2", username="duo_mem2", role="MEMBER", game_handle="mem2#1"),
            ],
        )
        db.add_all([team_solo, team_duo1, team_duo2])
        await db.commit()

        t_id = tourn.id
        solo_team_id = team_solo.id
        duo1_id = team_duo1.id
        duo2_id = team_duo2.id

    # 1. Registering with 1 member when min_team_size=2 must be rejected
    fail_res = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={"team_id": solo_team_id},
        headers=headers_cap1,
    )
    assert fail_res.status_code == 400
    assert "minimum of 2 players" in fail_res.json()["detail"]

    # 2. Registering with 2 members succeeds
    ok_res1 = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={"team_id": duo1_id},
        headers=headers_cap1,
    )
    assert ok_res1.status_code == 200

    # 3. Register second duo team
    ok_res2 = await client.post(
        f"/api/v1/tournaments/{t_id}/register",
        json={"team_id": duo2_id},
        headers=headers_cap2,
    )
    assert ok_res2.status_code == 200

    # 4. Organizer/Admin updates tournament status to "REGISTRATION_CLOSED"
    update_res = await client.put(
        f"/api/v1/organizer/tournaments/{t_id}",
        json={"status": "REGISTRATION_CLOSED"},
        headers=headers_admin,
    )
    assert update_res.status_code == 200
    assert update_res.json()["status"] == "REGISTRATION_CLOSED"

    # 5. Generate match structure & mark match live via admin
    gen_res = await client.post(
        f"/api/v1/admin/tournaments/{t_id}/generate-match-structure",
        json={"match_format": "SINGLE_ELIMINATION", "series_format": "BO3"},
        headers=headers_admin,
    )
    assert gen_res.status_code == 200

    matches_all = await client.get(f"/api/v1/matches?tournament_id={t_id}")
    assert matches_all.status_code == 200
    m_list = matches_all.json()
    assert len(m_list) >= 1
    m_id = m_list[0]["id"]

    mark_live_res = await client.post(
        f"/api/v1/admin/matches/{m_id}/mark-live",
        json={"youtube_live_url": "https://www.youtube.com/watch?v=live123"},
        headers=headers_admin,
    )
    assert mark_live_res.status_code == 200

    # 6. Fetch matches for this tournament and verify live match exists
    matches_res = await client.get(f"/api/v1/matches?tournament_id={t_id}&status=LIVE")
    assert matches_res.status_code == 200
    matches = matches_res.json()
    assert len(matches) >= 1
    assert matches[0]["status"] == "LIVE"
    assert matches[0]["team_a"]["name"] == "Duo Team One"
    assert matches[0]["team_b"]["name"] == "Duo Team Two"

