import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone

from app.main import app
from app.api.deps import get_db, require_super_admin, get_current_user
from app.models.user import User

@pytest.fixture
def mock_db():
    import mongomock_motor
    client = mongomock_motor.AsyncMongoMockClient()
    return client["test_ametheus"]

@pytest.fixture
def super_admin_user():
    return User(
        id="admin_123",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )

@pytest.fixture
def regular_player_user():
    return User(
        id="player_001",
        email="striker@gmail.com",
        username="striker_99",
        display_name="Striker 99",
        role="PLAYER",
    )

@pytest.mark.asyncio
async def test_initial_rating_500_and_registered_player_inclusion(mock_db, super_admin_user, regular_player_user):
    """
    Verifies that:
    1. Initial rating starts at 500 per game (not 1500).
    2. Registered players appear on the leaderboard even before playing a match.
    3. Super admin is excluded.
    """
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_super_admin] = lambda: super_admin_user
    app.dependency_overrides[get_current_user] = lambda: regular_player_user

    # Insert a game
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "Valorant",
        "category": "pc",
        "status": "ACTIVE",
    })

    # Insert regular registered player into db.users (0 matches, no team yet)
    await mock_db.users.insert_one(regular_player_user.to_mongo())

    # Insert super admin into db.users
    await mock_db.users.insert_one(super_admin_user.to_mongo())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/api/v1/leaderboard?game=valorant")
        assert res.status_code == 200
        data = res.json()

        # Regular player MUST appear even with 0 matches
        assert len(data) == 1
        player_entry = data[0]
        assert player_entry["id"] == "player_001"
        assert player_entry["rating"] == 500  # Initial rating 500!
        assert player_entry["matches_played"] == 0
        assert player_entry["wins"] == 0
        assert player_entry["win_rate"] == 0.0

        # Super admin must be purged
        assert all(e["id"] != "admin_123" for e in data)

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_manual_rating_management_workflow_and_reversal(mock_db, super_admin_user, regular_player_user):
    """
    Verifies the admin rating workflow:
    1. Get hierarchy: Game -> Tournament -> Round -> Match -> Participants.
    2. Adjust ratings bulk (+/- integer).
    3. Audit logs generated with reason, explanation, tournament metadata.
    4. Rating updates correctly in db and reflects on leaderboard.
    5. Reversal endpoint applies compensating transaction.
    6. Player-facing rating history.
    """
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_super_admin] = lambda: super_admin_user
    app.dependency_overrides[get_current_user] = lambda: regular_player_user

    # Setup game, tournament, match, team & player
    await mock_db.games.insert_one({
        "_id": "g_val",
        "id": "g_val",
        "slug": "valorant",
        "name": "Valorant",
        "category": "pc",
        "status": "ACTIVE",
    })
    await mock_db.users.insert_one(regular_player_user.to_mongo())

    team_doc = {
        "_id": "team_alpha",
        "id": "team_alpha",
        "name": "Team Alpha",
        "tag": "ALP",
        "captain_id": "player_001",
        "game_id": "valorant",
        "players": [{"user_id": "player_001", "role": "CAPTAIN"}],
        "status": "ACTIVE",
    }
    await mock_db.teams.insert_one(team_doc)

    tourn_doc = {
        "_id": "tourn_01",
        "id": "tourn_01",
        "slug": "val-champions",
        "title": "Valorant Champions Cup",
        "game_id": "valorant",
        "status": "LIVE",
        "stages": [
            {
                "id": "stage_01",
                "name": "Grand Finals Stage",
                "rounds": [
                    {
                        "id": "round_01",
                        "round_id": "round_01",
                        "name": "Grand Finals",
                        "status": "LIVE",
                    }
                ],
            }
        ],
    }
    await mock_db.tournaments.insert_one(tourn_doc)

    match_doc = {
        "_id": "match_01",
        "id": "match_01",
        "tournament_id": "tourn_01",
        "round_id": "round_01",
        "stage": "Grand Finals",
        "team_a_id": "team_alpha",
        "team_a_name": "Team Alpha",
        "team_b_name": "Team Beta",
        "status": "COMPLETED",
    }
    await mock_db.matches.insert_one(match_doc)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Hierarchy Check
        h_res = await ac.get("/api/v1/admin/rating-management/hierarchy")
        assert h_res.status_code == 200
        h_data = h_res.json()["hierarchy"]
        assert len(h_data) >= 1
        val_game = next(g for g in h_data if g["game_slug"] == "valorant")
        assert len(val_game["tournaments"]) == 1
        rnd = val_game["tournaments"][0]["rounds"][0]
        assert rnd["round_name"] == "Grand Finals"
        assert len(rnd["matches"]) == 1
        participant = rnd["matches"][0]["participants"][0]
        assert participant["player_id"] == "player_001"
        assert participant["current_rating"] == 500  # Initial 500

        # 2. Bulk Adjust Ratings (+60 for 1st place)
        adj_payload = {
            "adjustments": [
                {
                    "player_id": "player_001",
                    "game_id": "valorant",
                    "adjustment": 60,
                    "reason": "Placement",
                    "explanation": "1st place finish in Valorant Champions Cup Grand Finals",
                    "tournament_id": "tourn_01",
                    "round_id": "round_01",
                    "match_id": "match_01",
                }
            ]
        }
        post_res = await ac.post("/api/v1/admin/rating-management/adjust-bulk", json=adj_payload)
        assert post_res.status_code == 200
        logs = post_res.json()["audit_logs"]
        assert len(logs) == 1
        log_id = logs[0]["id"]
        assert logs[0]["previous_rating"] == 500
        assert logs[0]["adjustment"] == 60
        assert logs[0]["new_rating"] == 560
        assert logs[0]["tournament_name"] == "Valorant Champions Cup"

        # 3. Verify Leaderboard reflects 560 rating
        lb_res = await ac.get("/api/v1/leaderboard?game=valorant")
        assert lb_res.status_code == 200
        lb_data = lb_res.json()
        assert lb_data[0]["rating"] == 560

        # 4. Player-facing rating history
        my_history_res = await ac.get("/api/v1/leaderboard/ratings/me")
        assert my_history_res.status_code == 200
        hist_list = my_history_res.json()["history"]
        assert len(hist_list) == 1
        assert hist_list[0]["adjustment"] == 60
        assert hist_list[0]["new_rating"] == 560

        # 5. Admin Reversal (compensating transaction)
        rev_res = await ac.post(f"/api/v1/admin/rating-management/reverse/{log_id}", json={"reason": "Incorrect scoring entry"})
        assert rev_res.status_code == 200
        rev_log = rev_res.json()["reversal_log"]
        assert rev_log["is_reversal"] is True
        assert rev_log["adjustment"] == -60
        assert rev_log["new_rating"] == 500

        # Leaderboard rating restored to 500
        lb_res_after = await ac.get("/api/v1/leaderboard?game=valorant")
        assert lb_res_after.json()[0]["rating"] == 500

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_wallet_spending_category_with_topup_and_entries(mock_db, super_admin_user, regular_player_user):
    """
    Verifies that:
    1. A user who added ₹1,000 to their wallet appears on the 'spending' (Highest Spending) leaderboard with ₹1,000.
    2. They are NOT excluded with 'NO CONTENDERS REGISTERED'.
    3. Sorting by spending orders players correctly.
    """
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_super_admin] = lambda: super_admin_user
    app.dependency_overrides[get_current_user] = lambda: regular_player_user

    # Player 1 with ₹1,000 wallet top-up
    await mock_db.users.insert_one(regular_player_user.to_mongo())
    await mock_db.wallet_ledger.insert_one({
        "_id": "ledger_topup_1",
        "user_id": "player_001",
        "txn_type": "WALLET_TOPUP",
        "amount_paise": 100000,  # ₹1,000 in paise
        "description": "Wallet top-up via UPI",
        "created_at": datetime.now(timezone.utc),
    })

    # Player 2 with ₹2,500 entry fee expenditure
    player2 = User(
        id="player_002",
        email="champion@gmail.com",
        username="champion_gamer",
        display_name="Champion",
        role="PLAYER",
    )
    await mock_db.users.insert_one(player2.to_mongo())
    await mock_db.game_payments.insert_one({
        "_id": "pay_entry_2",
        "user_id": "player_002",
        "payment_type": "GAME_ENTRY",
        "amount_inr": 2500,
        "amount_paise": 250000,
        "status": "APPROVED",
        "created_at": datetime.now(timezone.utc),
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Rank by spending
        res = await ac.get("/api/v1/leaderboard?metric=spending")
        assert res.status_code == 200
        data = res.json()

        # Both players must appear!
        assert len(data) == 2
        # Player 2 with ₹2,500 is Rank 1
        assert data[0]["id"] == "player_002"
        assert data[0]["spending"] == 2500
        assert data[0]["rank"] == 1

        # Player 1 with ₹1,000 is Rank 2
        assert data[1]["id"] == "player_001"
        assert data[1]["spending"] == 1000
        assert data[1]["rank"] == 2

    app.dependency_overrides.clear()
