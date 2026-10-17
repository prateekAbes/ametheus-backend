import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_super_admin_strictly_excluded_from_all_leaderboard_views(client, mock_db):
    # 1. Setup game
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "Valorant",
        "category": "pc",
        "status": "ACTIVE",
    })

    # 2. Setup Super Admin users
    await mock_db.users.insert_one({
        "_id": "u_sa1",
        "id": "u_sa1",
        "email": "super_admin@ametheus.gg",
        "username": "super_admin_boss",
        "display_name": "Super Admin Boss",
        "role": "SUPER_ADMIN",
    })
    await mock_db.users.insert_one({
        "_id": "u_sa2",
        "id": "u_sa2",
        "email": "admin@ametheus.gg",
        "username": "root_admin",
        "display_name": "Root Admin",
        "role": "ADMIN",
    })

    # 3. Setup Legitimate Contenders (Player & Organizer)
    await mock_db.users.insert_one({
        "_id": "u_player1",
        "id": "u_player1",
        "email": "champion@ametheus.gg",
        "username": "champion_player",
        "display_name": "Champion Contender",
        "role": "PLAYER",
    })
    await mock_db.users.insert_one({
        "_id": "u_org1",
        "id": "u_org1",
        "email": "organizer_comp@ametheus.gg",
        "username": "pro_organizer",
        "display_name": "Pro Organizer",
        "role": "ORGANIZER",
    })

    # 4. Setup Teams
    # Super Admin Team has huge score (would be #1 without exclusion)
    await mock_db.teams.insert_one({
        "_id": "team_sa",
        "id": "team_sa",
        "name": "Super Admin Dominators",
        "tag": "SAD",
        "game_id": "game_val",
        "captain_id": "u_sa1",
        "status": "ACTIVE",
    })
    # Player Team
    await mock_db.teams.insert_one({
        "_id": "team_p1",
        "id": "team_p1",
        "name": "Team Champion",
        "tag": "CHAMP",
        "game_id": "game_val",
        "captain_id": "u_player1",
        "status": "ACTIVE",
    })
    # Organizer Team
    await mock_db.teams.insert_one({
        "_id": "team_org",
        "id": "team_org",
        "name": "Team Organizer",
        "tag": "ORG",
        "game_id": "game_val",
        "captain_id": "u_org1",
        "status": "ACTIVE",
    })

    # Matches for Super Admin (10 wins)
    for i in range(10):
        await mock_db.matches.insert_one({
            "_id": f"m_sa_{i}",
            "team_a_id": "team_sa",
            "team_b_id": "other_team",
            "winner_team_id": "team_sa",
            "status": "COMPLETED",
        })
    # Matches for Player (5 wins)
    for i in range(5):
        await mock_db.matches.insert_one({
            "_id": f"m_p_{i}",
            "team_a_id": "team_p1",
            "team_b_id": "other_team",
            "winner_team_id": "team_p1",
            "status": "COMPLETED",
        })
    # Matches for Organizer (2 wins)
    for i in range(2):
        await mock_db.matches.insert_one({
            "_id": f"m_org_{i}",
            "team_a_id": "team_org",
            "team_b_id": "other_team",
            "winner_team_id": "team_org",
            "status": "COMPLETED",
        })

    # 5. TEST: GET /api/v1/leaderboard
    res = await client.get("/api/v1/leaderboard")
    assert res.status_code == 200
    entries = res.json()

    # Verify Super Admin is completely absent
    for e in entries:
        assert "super_admin" not in str(e).lower()
        assert "root_admin" not in str(e).lower()
        assert e["id"] != "u_sa1"
        assert e["id"] != "team_sa"
        assert e["name"] != "Super Admin Dominators"

    # Verify Legitimate contenders are present and rank consecutively from 1
    assert len(entries) == 2
    assert entries[0]["rank"] == 1
    assert entries[0]["name"] == "Team Champion"
    assert entries[1]["rank"] == 2
    assert entries[1]["name"] == "Team Organizer"

    # 6. TEST: Across all time ranges
    for tr in ["all_time", "season", "month", "week"]:
        r_tr = await client.get(f"/api/v1/leaderboard?time_range={tr}")
        assert r_tr.status_code == 200
        tr_entries = r_tr.json()
        assert all("super_admin" not in str(e).lower() for e in tr_entries)
        assert all(e["id"] != "team_sa" for e in tr_entries)

    # 7. TEST: Across all metrics
    metrics = ["overall", "win_rate", "wins", "earnings", "spending", "streak", "matches", "top3"]
    for m in metrics:
        r_m = await client.get(f"/api/v1/leaderboard?metric={m}")
        assert r_m.status_code == 200
        m_entries = r_m.json()
        assert all("super_admin" not in str(e).lower() for e in m_entries)
        assert all(e["id"] != "team_sa" for e in m_entries)

    # 8. TEST: Search filter
    r_search = await client.get("/api/v1/leaderboard?search=super_admin")
    assert r_search.status_code == 200
    assert len(r_search.json()) == 0

    r_search_champ = await client.get("/api/v1/leaderboard?search=champion")
    assert r_search_champ.status_code == 200
    assert len(r_search_champ.json()) == 1
    assert r_search_champ.json()[0]["name"] == "Team Champion"

    # 9. TEST: Platform Stats exclude Super Admin
    r_stats = await client.get("/api/v1/leaderboard/stats")
    assert r_stats.status_code == 200
    stats = r_stats.json()
    # Only 2 legitimate teams counted, not 3!
    assert stats["ranked_players"] == 2
