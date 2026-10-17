import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
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
async def test_single_super_admin_returns_zero_entries_and_zero_stats(client, mock_db):
    """
    Scenario 1:
    The database contains ONLY the Super Admin account (and their team).
    Leaderboard must return 0 entries and 0 platform stats (no fallback/mock records).
    """
    await mock_db.users.insert_one({
        "_id": "super_admin_id",
        "id": "super_admin_id",
        "email": "admin@ametheus.gg",
        "username": "itachi",
        "display_name": "SUPER_ADMIN",
        "role": "SUPER_ADMIN",
        "is_deleted": False,
    })
    await mock_db.teams.insert_one({
        "_id": "sa_team_id",
        "id": "sa_team_id",
        "name": "fbjbjfha",
        "tag": "SA",
        "captain_id": "super_admin_id",
        "captain": "@itachi",
        "status": "DISBANDED",
        "is_disbanded": True,
    })

    # Test /api/v1/leaderboard
    res = await client.get("/api/v1/leaderboard")
    assert res.status_code == 200
    entries = res.json()
    assert len(entries) == 0, f"Expected 0 entries when only Super Admin exists, got {entries}"

    # Test /api/v1/leaderboard/stats
    stats_res = await client.get("/api/v1/leaderboard/stats")
    assert stats_res.status_code == 200
    stats = stats_res.json()
    assert stats["ranked_players"] == 0
    assert stats["matches_played"] == 0
    assert stats["prize_paid_out"] == "$0"

@pytest.mark.asyncio
async def test_orphaned_disbanded_team_adhjahdj_excluded(client, mock_db):
    """
    Scenario 2:
    A user created a team 'adhjahdj' and was later deleted.
    The team has 0 completed matches and 0 prize earnings.
    It MUST be excluded from the leaderboard standing.
    """
    # Orphaned team with no user in db.users
    await mock_db.teams.insert_one({
        "_id": "5f9d1019-d07d-4d6f-9bb9-1ed4f6752f44",
        "id": "5f9d1019-d07d-4d6f-9bb9-1ed4f6752f44",
        "name": "adhjahdj",
        "tag": "DNVA",
        "captain_id": "deleted_user_id_123",
        "captain": "@captain",
        "status": "DISBANDED",
        "is_disbanded": True,
        "is_ineligible": True,
        "is_deleted_account": True,
    })

    res = await client.get("/api/v1/leaderboard")
    assert res.status_code == 200
    entries = res.json()
    assert len(entries) == 0
    assert not any(e["name"] == "adhjahdj" for e in entries)

@pytest.mark.asyncio
async def test_dynamic_metric_sorting_across_all_metrics(client, mock_db):
    """
    Scenario 3:
    Multiple legitimate players with varied stats.
    Verify dynamic sort order for:
    - rating / overall
    - win_rate
    - wins
    - streak
    - matches_played
    - earnings
    - spending
    """
    now = datetime.now(timezone.utc)

    # 1. Player Alpha: high rating & wins, moderate earnings
    await mock_db.users.insert_one({
        "_id": "user_alpha",
        "id": "user_alpha",
        "username": "alpha_pro",
        "display_name": "Alpha Pro",
        "role": "USER",
    })
    await mock_db.teams.insert_one({
        "_id": "team_alpha",
        "id": "team_alpha",
        "name": "Alpha Squad",
        "tag": "ALPH",
        "captain_id": "user_alpha",
        "status": "ACTIVE",
        "is_disbanded": False,
    })
    # 5 matches: 4 wins, 1 loss (80% win rate)
    for i in range(4):
        await mock_db.matches.insert_one({
            "_id": f"m_a_{i}",
            "team_a_id": "team_alpha",
            "team_b_id": "team_dummy",
            "winner_team_id": "team_alpha",
            "status": "COMPLETED",
            "completed_at": (now - timedelta(days=2)).isoformat(),
        })
    await mock_db.matches.insert_one({
        "_id": "m_a_loss",
        "team_a_id": "team_alpha",
        "team_b_id": "team_dummy",
        "winner_team_id": "team_dummy",
        "status": "COMPLETED",
        "completed_at": (now - timedelta(days=1)).isoformat(),
    })

    # 2. Player Beta: 100% win rate (2/2), highest streak (2)
    await mock_db.users.insert_one({
        "_id": "user_beta",
        "id": "user_beta",
        "username": "beta_sniper",
        "display_name": "Beta Sniper",
        "role": "USER",
    })
    await mock_db.teams.insert_one({
        "_id": "team_beta",
        "id": "team_beta",
        "name": "Beta Clan",
        "tag": "BETA",
        "captain_id": "user_beta",
        "status": "ACTIVE",
        "is_disbanded": False,
    })
    for i in range(2):
        await mock_db.matches.insert_one({
            "_id": f"m_b_{i}",
            "team_a_id": "team_beta",
            "team_b_id": "team_dummy",
            "winner_team_id": "team_beta",
            "status": "COMPLETED",
            "completed_at": (now - timedelta(days=1)).isoformat(),
        })

    # 3. Player Gamma: Highest spending (1500) and highest earnings (8000)
    await mock_db.users.insert_one({
        "_id": "user_gamma",
        "id": "user_gamma",
        "username": "gamma_whale",
        "display_name": "Gamma Whale",
        "role": "USER",
    })
    await mock_db.teams.insert_one({
        "_id": "team_gamma",
        "id": "team_gamma",
        "name": "Gamma Titans",
        "tag": "GAMM",
        "captain_id": "user_gamma",
        "status": "ACTIVE",
        "is_disbanded": False,
    })
    await mock_db.game_payments.insert_one({
        "_id": "pay_gamma_1",
        "team_id": "team_gamma",
        "user_id": "user_gamma",
        "amount_inr": 1500,
        "payment_type": "TOURNAMENT_ENTRY",
        "status": "APPROVED",
        "verified_at": (now - timedelta(days=3)).isoformat(),
    })
    await mock_db.payouts.insert_one({
        "_id": "payout_gamma_1",
        "team_id": "team_gamma",
        "user_id": "user_gamma",
        "amount_paise": 800000, # 8000 INR
        "rank_or_placement": "1st Place",
        "status": "PAID",
        "paid_at": (now - timedelta(days=3)).isoformat(),
    })

    # Test sorting by rating (Overall) -> Alpha Pro (4 wins) has rating 1500 + 4*25 - 10 = 1590
    res_overall = await client.get("/api/v1/leaderboard?metric=overall")
    assert res_overall.status_code == 200
    overall_list = res_overall.json()
    assert len(overall_list) == 3
    assert overall_list[0]["display_name"] == "Alpha Pro"

    # Test sorting by win_rate -> Beta Sniper (100.0%) is 1st
    res_wr = await client.get("/api/v1/leaderboard?metric=win_rate")
    assert res_wr.status_code == 200
    wr_list = res_wr.json()
    assert wr_list[0]["display_name"] == "Beta Sniper"
    assert wr_list[0]["win_rate"] == 100.0

    # Test sorting by wins -> Alpha Pro (4 wins) is 1st
    res_wins = await client.get("/api/v1/leaderboard?metric=wins")
    assert res_wins.status_code == 200
    wins_list = res_wins.json()
    assert wins_list[0]["display_name"] == "Alpha Pro"
    assert wins_list[0]["wins"] == 4

    # Test sorting by spending -> Gamma Whale is 1st
    res_spending = await client.get("/api/v1/leaderboard?metric=spending")
    assert res_spending.status_code == 200
    spending_list = res_spending.json()
    assert spending_list[0]["display_name"] == "Gamma Whale"
    assert spending_list[0]["spending"] == 1500

    # Test sorting by earnings -> Gamma Whale is 1st
    res_earnings = await client.get("/api/v1/leaderboard?metric=earnings")
    assert res_earnings.status_code == 200
    earnings_list = res_earnings.json()
    assert earnings_list[0]["display_name"] == "Gamma Whale"
    assert earnings_list[0]["earnings"] == 8000

    # Test sorting by streak -> Beta Sniper is 1st (2 streak vs 0 for Alpha who lost last match)
    res_streak = await client.get("/api/v1/leaderboard?metric=streak")
    assert res_streak.status_code == 200
    streak_list = res_streak.json()
    assert streak_list[0]["display_name"] == "Beta Sniper"
    assert streak_list[0]["streak"] == 2

    # Test sorting by matches_played -> Alpha Pro (5 matches) is 1st
    res_matches = await client.get("/api/v1/leaderboard?metric=matches")
    assert res_matches.status_code == 200
    matches_list = res_matches.json()
    assert matches_list[0]["display_name"] == "Alpha Pro"
    assert matches_list[0]["matches_played"] == 5
