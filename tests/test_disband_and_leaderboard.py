import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin, require_organizer

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    
    captain_user = User(
        id="cap_123",
        clerk_user_id="user_cap_123",
        email="captain@ametheus.gg",
        username="captain_alpha",
        role="USER",
    )
    app.dependency_overrides[get_current_user] = lambda: captain_user
    app.dependency_overrides[require_super_admin] = lambda: captain_user
    app.dependency_overrides[require_organizer] = lambda: captain_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_team_disband_soft_delete_and_leaderboard_preservation(client, mock_db):
    # 1. Setup a game title
    await mock_db.games.insert_one({
        "_id": "game_bgmi",
        "id": "game_bgmi",
        "slug": "bgmi",
        "name": "Battlegrounds Mobile India",
        "is_team_game": True,
        "max_team_size": 4,
        "min_team_size": 4,
        "status": "ACTIVE",
    })

    # 2. Insert a team led by captain_alpha
    team_doc = {
        "_id": "team_alpha_id",
        "id": "team_alpha_id",
        "name": "Team Alpha",
        "tag": "ALPH",
        "game_id": "game_bgmi",
        "captain_id": "cap_123",
        "invite_code": "ALPHA1",
        "status": "ACTIVE",
        "is_disbanded": False,
        "members": [
            {"user_id": "cap_123", "role": "CAPTAIN", "game_handle": "AlphaLeader"}
        ],
        "substitutes": [],
        "payment_status": "PAID",
    }
    await mock_db.teams.insert_one(team_doc)

    # 3. Insert completed match records for this team
    await mock_db.matches.insert_one({
        "_id": "m1",
        "team_a_id": "team_alpha_id",
        "team_b_id": "other_team",
        "winner_team_id": "team_alpha_id",
        "status": "COMPLETED",
    })
    await mock_db.matches.insert_one({
        "_id": "m2",
        "team_a_id": "other_team_2",
        "team_b_id": "team_alpha_id",
        "winner_team_id": "team_alpha_id",
        "status": "COMPLETED",
    })

    # 4. Insert verified payout earnings
    await mock_db.payouts.insert_one({
        "_id": "pay1",
        "team_id": "team_alpha_id",
        "amount_paise": 500000,  # ₹5,000
        "status": "PAID",
    })

    # 5. Verify the active team appears in /teams and /teams/my-teams
    res = await client.get("/api/v1/teams?game_slug=bgmi")
    assert res.status_code == 200
    teams_list = res.json()
    assert len(teams_list) == 1
    assert teams_list[0]["name"] == "Team Alpha"

    my_res = await client.get("/api/v1/teams/my-teams")
    assert my_res.status_code == 200
    assert len(my_res.json()) == 1

    # 6. Disband the team
    del_res = await client.delete("/api/v1/teams/team_alpha_id")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"

    # 7. Check database: team document still EXISTS but marked DISBANDED
    db_team = await mock_db.teams.find_one({"_id": "team_alpha_id"})
    assert db_team is not None
    assert db_team["status"] == "DISBANDED"
    assert db_team["is_disbanded"] is True
    assert db_team["disbanded_by"] == "captain_alpha"

    # 8. Check that active team queries now exclude this disbanded team
    res_after = await client.get("/api/v1/teams?game_slug=bgmi")
    assert len(res_after.json()) == 0

    my_after = await client.get("/api/v1/teams/my-teams")
    assert len(my_after.json()) == 0

    # 9. Check that captain can now create a new team for the same game
    create_payload = {
        "game_slug": "bgmi",
        "name": "Team Phoenix",
        "tag": "PHNX",
        "captain_handle": "PhoenixLeader",
    }
    create_res = await client.post("/api/v1/teams", json=create_payload)
    assert create_res.status_code == 201
    assert create_res.json()["name"] == "Team Phoenix"

    # 10. Check Leaderboard: Team Alpha's historical records, wins, and earnings MUST be preserved!
    lb_res = await client.get("/api/v1/leaderboard?game=bgmi")
    assert lb_res.status_code == 200
    entries = lb_res.json()
    assert len(entries) >= 1
    alpha_entry = next((e for e in entries if e["name"] == "Team Alpha"), None)
    assert alpha_entry is not None
    assert alpha_entry["is_disbanded"] is True
    assert alpha_entry["status"] == "DISBANDED"
    assert alpha_entry["wins"] == 2
    assert alpha_entry["earnings"] == 5000
    assert alpha_entry["rating"] > 1500

@pytest.mark.asyncio
async def test_leaderboard_spending_and_default_categories(client, mock_db):
    # Setup team and payments
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "Valorant",
        "category": "pc",
        "is_team_game": True,
        "max_team_size": 5,
        "min_team_size": 5,
        "status": "ACTIVE",
    })
    await mock_db.teams.insert_one({
        "_id": "team_beta",
        "id": "team_beta",
        "name": "Team Beta",
        "tag": "BETA",
        "game_id": "game_val",
        "captain_id": "cap_beta",
        "status": "ACTIVE",
        "is_disbanded": False,
        "payment_status": "PAID",
    })
    # Approved payment
    await mock_db.game_payments.insert_one({
        "_id": "pay_beta_1",
        "team_id": "team_beta",
        "user_id": "cap_beta",
        "amount_inr": 450,
        "amount_paise": 45000,
        "status": "APPROVED",
        "is_dummy": False,
    })

    # 1. Categories endpoint returns spending category
    cat_res = await client.get("/api/v1/leaderboard/categories")
    assert cat_res.status_code == 200
    cats = cat_res.json()
    spending_cat = next((c for c in cats if c["slug"] == "spending"), None)
    assert spending_cat is not None
    assert spending_cat["sort_metric"] == "spending"

    # 2. Leaderboard endpoint returns entry with spending
    lb_res = await client.get("/api/v1/leaderboard?category=spending")
    assert lb_res.status_code == 200
    entries = lb_res.json()
    beta_entry = next((e for e in entries if e["id"] == "team_beta"), None)
    assert beta_entry is not None
    assert beta_entry["spending"] == 450

