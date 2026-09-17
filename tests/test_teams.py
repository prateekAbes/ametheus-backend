import pytest
from tests.conftest import make_auth_headers, TestSessionLocal
from app.models.user import User
from app.models.game import Game
from sqlalchemy import select

@pytest.mark.asyncio
async def test_team_create_and_join_flow(client):
    headers_captain = make_auth_headers("usr_test_captain")
    headers_member = make_auth_headers("usr_test_teammate")

    # Ensure users exist in DB
    async with TestSessionLocal() as db:
        res_c = await db.execute(select(User).where(User.id == "usr_test_captain"))
        if not res_c.scalars().first():
            c = User(id="usr_test_captain", clerk_user_id="usr_test_captain", email="c@test.com", username="test_captain", role="PLAYER")
            m = User(id="usr_test_teammate", clerk_user_id="usr_test_teammate", email="m@test.com", username="test_teammate", role="PLAYER")
            db.add_all([c, m])
            await db.commit()

        # Ensure valorant game exists
        res_g = await db.execute(select(Game).where(Game.slug == "valorant"))
        if not res_g.scalars().first():
            g = Game(
                slug="valorant",
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
    headers_captain = make_auth_headers("usr_test_captain")
    headers_member = make_auth_headers("usr_test_teammate")

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
