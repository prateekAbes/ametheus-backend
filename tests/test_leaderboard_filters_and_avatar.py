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
async def test_leaderboard_metrics_and_platform_filters(client, mock_db):
    # Setup 2 games: 1 PC, 1 Mobile
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "Valorant",
        "category": "pc",
        "status": "ACTIVE",
    })
    await mock_db.games.insert_one({
        "_id": "game_bgmi",
        "id": "game_bgmi",
        "slug": "bgmi",
        "name": "BGMI",
        "category": "mobile",
        "status": "ACTIVE",
    })

    # Setup 3 users with avatar_urls
    await mock_db.users.insert_one({
        "_id": "u1",
        "id": "u1",
        "username": "ace_player",
        "avatar_url": "https://cdn.ametheus.gg/avatars/ace.png",
    })
    await mock_db.users.insert_one({
        "_id": "u2",
        "id": "u2",
        "username": "blaze_player",
        "avatar_url": "https://cdn.ametheus.gg/avatars/blaze.png",
    })
    await mock_db.users.insert_one({
        "_id": "u3",
        "id": "u3",
        "username": "cypher_player",
        "avatar_url": None,
    })

    # Setup 3 teams
    await mock_db.teams.insert_one({
        "_id": "t1",
        "id": "t1",
        "name": "Team Ace",
        "tag": "ACE",
        "game_id": "game_val",
        "captain_id": "u1",
        "status": "ACTIVE",
    })
    await mock_db.teams.insert_one({
        "_id": "t2",
        "id": "t2",
        "name": "Team Blaze",
        "tag": "BLZ",
        "game_id": "game_bgmi",
        "captain_id": "u2",
        "status": "ACTIVE",
    })
    await mock_db.teams.insert_one({
        "_id": "t3",
        "id": "t3",
        "name": "Team Cypher",
        "tag": "CYP",
        "game_id": "game_val",
        "captain_id": "u3",
        "logo_url": "https://cdn.ametheus.gg/logos/cypher.png",
        "status": "ACTIVE",
    })

    # Completed matches:
    # Team Ace: 4 matches, 3 wins then 1 loss (loss is latest -> streak 0)
    for i in range(3):
        await mock_db.matches.insert_one({
            "_id": f"m_ace_win_{i}",
            "team_a_id": "t1",
            "team_b_id": "t3",
            "winner_team_id": "t1",
            "status": "COMPLETED",
            "completed_at": f"2026-10-10T10:0{i}:00Z",
        })
    await mock_db.matches.insert_one({
        "_id": "m_ace_loss",
        "team_a_id": "t1",
        "team_b_id": "other",
        "winner_team_id": "other",
        "status": "COMPLETED",
        "completed_at": "2026-10-12T10:00:00Z",
    })

    # Team Blaze: 2 matches, 2 wins (100% win rate, streak 2)
    for i in range(2):
        await mock_db.matches.insert_one({
            "_id": f"m_blz_win_{i}",
            "team_a_id": "t2",
            "team_b_id": "other",
            "winner_team_id": "t2",
            "status": "COMPLETED",
            "completed_at": f"2026-10-11T10:0{i}:00Z",
        })

    # Payments: Team Cypher spent ₹3,000, Team Ace spent ₹1,000
    await mock_db.game_payments.insert_one({
        "_id": "p1",
        "team_id": "t3",
        "user_id": "u3",
        "amount_inr": 3000,
        "status": "APPROVED",
    })
    await mock_db.game_payments.insert_one({
        "_id": "p2",
        "team_id": "t1",
        "user_id": "u1",
        "amount_inr": 1000,
        "status": "APPROVED",
    })

    # 1. Overall Standing (by rating)
    res_overall = await client.get("/api/v1/leaderboard?metric=overall")
    assert res_overall.status_code == 200
    data_overall = res_overall.json()
    assert len(data_overall) == 3
    # Check avatar_url populated
    ace = next(e for e in data_overall if e["name"] == "Team Ace")
    assert ace["avatar_url"] == "https://cdn.ametheus.gg/avatars/ace.png"
    cypher = next(e for e in data_overall if e["name"] == "Team Cypher")
    assert cypher["avatar_url"] == "https://cdn.ametheus.gg/logos/cypher.png"

    # 2. Highest Win Rate: Blaze has 100%, Ace has 75%
    res_wr = await client.get("/api/v1/leaderboard?metric=win_rate")
    assert res_wr.status_code == 200
    data_wr = res_wr.json()
    assert data_wr[0]["name"] == "Team Blaze"
    assert data_wr[0]["win_rate"] == 100.0

    # 3. Most Match Wins: Ace has 3 wins, Blaze has 2 wins
    res_wins = await client.get("/api/v1/leaderboard?metric=wins")
    assert res_wins.status_code == 200
    data_wins = res_wins.json()
    assert data_wins[0]["name"] == "Team Ace"
    assert data_wins[0]["wins"] == 3

    # 4. Highest Spending: Cypher has 3000, Ace has 1000, Blaze has 0
    res_spending = await client.get("/api/v1/leaderboard?metric=spending")
    assert res_spending.status_code == 200
    data_spending = res_spending.json()
    assert data_spending[0]["name"] == "Team Cypher"
    assert data_spending[0]["spending"] == 3000

    # 5. Platform Filter: PC only returns Valorant teams (Ace, Cypher)
    res_pc = await client.get("/api/v1/leaderboard?platform=pc")
    assert res_pc.status_code == 200
    data_pc = res_pc.json()
    assert len(data_pc) == 2
    assert all(e["category"] == "pc" for e in data_pc)

    # 6. Combined Filter: PC + Most Match Wins
    res_pc_wins = await client.get("/api/v1/leaderboard?platform=pc&metric=wins")
    assert res_pc_wins.status_code == 200
    data_pc_wins = res_pc_wins.json()
    assert len(data_pc_wins) == 2
    assert data_pc_wins[0]["name"] == "Team Ace"

    # 7. Win Streak Metric: Blaze won 2 in a row -> streak 2
    res_streak = await client.get("/api/v1/leaderboard?metric=streak")
    assert res_streak.status_code == 200
    data_streak = res_streak.json()
    assert data_streak[0]["name"] == "Team Blaze"
    assert data_streak[0]["streak"] == 2

    # 8. Matches Played Metric: Ace played 4 matches
    res_matches = await client.get("/api/v1/leaderboard?metric=matches")
    assert res_matches.status_code == 200
    data_matches = res_matches.json()
    assert data_matches[0]["name"] == "Team Ace"
    assert data_matches[0]["matches_played"] == 4

    # 9. Platform Stats Endpoint
    res_stats = await client.get("/api/v1/leaderboard/stats")
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["ranked_players"] == 3
    assert stats["matches_played"] == 6
    assert stats["active_streaks"] == 1

    # 10. Time Range Filtering on Leaderboard and Stats
    res_week = await client.get("/api/v1/leaderboard?time_range=week")
    assert res_week.status_code == 200
    res_week_stats = await client.get("/api/v1/leaderboard/stats?time_range=week")
    assert res_week_stats.status_code == 200

    # 11. Fragment name resolution (ADX tag should resolve to captain's username/display_name)
    await mock_db.teams.insert_one({
        "_id": "t_adx",
        "id": "t_adx",
        "name": "ADX",
        "tag": "ADX",
        "game_id": "game_val",
        "captain_id": "u1",
        "status": "ACTIVE",
    })
    res_frag = await client.get("/api/v1/leaderboard?metric=overall")
    assert res_frag.status_code == 200
    adx_entry = next((e for e in res_frag.json() if e["id"] == "u1" or e.get("tag") == "ADX"), None)
    assert adx_entry is not None
    # Must resolve to the captain's username "ace_player", NEVER the fragment "ADX"
    assert adx_entry["name"] == "ace_player"
    assert adx_entry["username"] == "ace_player"
