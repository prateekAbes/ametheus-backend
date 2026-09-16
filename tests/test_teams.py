import pytest

@pytest.mark.asyncio
async def test_team_create_and_join_flow(client):
    # 1. Create a game first
    # Using seeded or direct
    headers_captain = {"x-user-id": "usr_test_captain"}
    
    # Create Team
    payload = {
        "game_slug": "valorant",
        "name": "Vortex Elite",
        "tag": "VTX",
        "captain_handle": "Vortex#123",
    }
    
    # If game doesn't exist yet, seed it in test
    from app.models.game import Game
    from tests.conftest import TestSessionLocal
    async with TestSessionLocal() as db:
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

    resp = await client.post("/api/v1/teams", json=payload, headers=headers_captain)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "Vortex Elite"
    assert data["tag"] == "VTX"
    assert len(data["invite_code"]) == 6  # 6-character unique code rule
    assert data["invite_code"].isupper()
    
    invite_code = data["invite_code"]

    # 2. Join Team via 6-character code (Rule 3.3: ₹0 join fee)
    headers_member = {"x-user-id": "usr_test_teammate"}
    join_payload = {
        "invite_code": invite_code,
        "player_handle": "Teammate#999",
    }
    join_resp = await client.post("/api/v1/teams/join", json=join_payload, headers=headers_member)
    assert join_resp.status_code == 200, join_resp.text
    join_data = join_resp.json()
    assert len(join_data["members"]) == 2
