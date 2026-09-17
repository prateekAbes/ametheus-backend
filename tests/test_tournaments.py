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
        u = User(id="usr_tourn_captain", clerk_user_id="usr_tourn_captain", email="tc@test.com", username="tourn_captain", role="PLAYER")
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
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)

        team = Team(
            game_id=g.id,
            captain_id="usr_tourn_captain",
            name="Alpha Squad",
            tag="ALP",
            invite_code="ALP123",
            max_members=5,
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
