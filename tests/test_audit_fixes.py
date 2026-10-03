import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    
    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_123",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_public_platform_stats(client, mock_db):
    """Verify that /api/v1/platform/stats returns platform counts publicly."""
    await mock_db.users.insert_one({"_id": "u1", "username": "player1"})
    await mock_db.tournaments.insert_one({"_id": "t1", "title": "Cup 1"})
    await mock_db.games.insert_one({"_id": "g1", "slug": "valorant", "status": "active"})

    resp = await client.get("/api/v1/platform/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_users" in data
    assert "total_tournaments" in data
    assert "total_games" in data
    assert data["total_users"] == 1
    assert data["total_tournaments"] == 1
    assert data["total_games"] == 1

@pytest.mark.asyncio
async def test_wallet_ledger_formatting(client, mock_db):
    """Verify that /api/v1/wallet/ledger populates entry_type, source_type, and absolute amount_inr."""
    await mock_db.wallet_ledger.insert_one({
        "_id": "w1",
        "user_id": "admin_123",
        "txn_type": "WALLET_TOPUP",
        "amount_paise": 50000,
        "balance_before_paise": 0,
        "balance_after_paise": 50000,
        "created_at": "2026-10-02T12:00:00Z",
    })
    await mock_db.wallet_ledger.insert_one({
        "_id": "w2",
        "user_id": "admin_123",
        "txn_type": "GAME_ENTRY_PAYMENT",
        "amount_paise": -10000,
        "balance_before_paise": 50000,
        "balance_after_paise": 40000,
        "created_at": "2026-10-02T13:00:00Z",
    })

    resp = await client.get("/api/v1/wallet/ledger")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    # Check topup entry
    topup = next(t for t in data if t["txn_type"] == "WALLET_TOPUP")
    assert topup["entry_type"] == "CREDIT"
    assert topup["source_type"] == "WALLET_TOPUP"
    assert topup["amount_inr"] == 500.0
    assert topup["balance_after_inr"] == 500.0

    # Check debit entry
    debit = next(t for t in data if t["txn_type"] == "GAME_ENTRY_PAYMENT")
    assert debit["entry_type"] == "DEBIT"
    assert debit["source_type"] == "GAME_ENTRY_PAYMENT"
    assert debit["amount_inr"] == 100.0
    assert debit["balance_after_inr"] == 400.0

@pytest.mark.asyncio
async def test_match_patch_credentials(client, mock_db):
    """Verify that PATCH /api/v1/matches/{id} updates lobby credentials and stream url."""
    await mock_db.matches.insert_one({
        "_id": "match_99",
        "tournament_id": "tourn_1",
        "stage": "Finals",
        "status": "LIVE",
    })

    patch_payload = {
        "lobby_code": "ROOM-TEST-88",
        "lobby_password": "SECRET_PASS_99",
        "youtube_live_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    resp = await client.patch("/api/v1/matches/match_99", json=patch_payload)
    assert resp.status_code == 200

    doc = await mock_db.matches.find_one({"_id": "match_99"})
    assert doc["lobby_code"] == "ROOM-TEST-88"
    assert doc["lobby_password"] == "SECRET_PASS_99"
    assert doc["youtube_live_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert doc["live_link"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

@pytest.mark.asyncio
async def test_svg_sanitization(client, mock_db):
    """Verify that SVG upload strips script tags and malicious attributes."""
    from app.api.v1.endpoints.media import sanitize_svg
    malicious_svg = b'<svg><script>alert("xss")</script><rect width="100" height="100" onload="alert(1)" /></svg>'
    cleaned = sanitize_svg(malicious_svg)
    assert b"<script" not in cleaned
    assert b"onload" not in cleaned
    assert b'<rect width="100" height="100"' in cleaned

@pytest.mark.asyncio
async def test_game_category_normalization(client, mock_db):
    """Verify that game creation normalizes category slug."""
    payload = {
        "slug": "apex-legends",
        "name": "Apex Legends",
        "category": "BR",
        "platform": "PC",
        "tag": "APEX",
        "banner_url": "https://example.com/banner.png",
        "logo_url": "https://example.com/logo.png",
    }
    resp = await client.post("/api/v1/games", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["category"] == "battle-royale"


@pytest.mark.asyncio
async def test_team_creation_and_join_resilience(client, mock_db):
    """Verify that team creation and joining work seamlessly without 422 with both canonical and alias fields."""
    await mock_db.games.insert_one({
        "_id": "g_val",
        "slug": "valorant",
        "name": "Valorant",
        "is_team_game": True,
        "max_team_size": 5,
        "max_substitutes": 1,
        "status": "ACTIVE",
        "entry_fee_inr": 0,
    })
    await mock_db.games.insert_one({
        "_id": "g_bgmi",
        "slug": "bgmi",
        "name": "BGMI",
        "is_team_game": True,
        "max_team_size": 4,
        "max_substitutes": 1,
        "status": "ACTIVE",
        "entry_fee_inr": 0,
    })

    # 1. Create team using alias fields (game_id and game_handle)
    alias_payload = {
        "name": "Velocity Strike",
        "tag": "VSE",
        "game_id": "valorant",
        "game_handle": "vortex#IND",
    }
    resp1 = await client.post("/api/v1/teams", json=alias_payload)
    assert resp1.status_code == 201, f"Expected 201 Created but got {resp1.status_code}: {resp1.text}"
    team1_data = resp1.json()
    assert team1_data["name"] == "Velocity Strike"
    assert team1_data["tag"] == "VSE"
    assert team1_data["game_slug"] == "valorant"
    assert len(team1_data["members"]) == 1
    assert team1_data["members"][0]["game_handle"] == "vortex#IND"
    invite_code = team1_data["invite_code"]

    # 2. Join team using game_handle alias
    other_user = User(
        id="user_player_2",
        clerk_user_id="clerk_p2",
        email="p2@ametheus.gg",
        username="contender_two",
        role="PLAYER",
    )
    app.dependency_overrides[get_current_user] = lambda: other_user
    join_payload = {
        "invite_code": invite_code,
        "game_handle": "p2#IND",
    }
    resp_join = await client.post("/api/v1/teams/join", json=join_payload)
    assert resp_join.status_code == 200, f"Expected 200 OK but got {resp_join.status_code}: {resp_join.text}"
    joined_data = resp_join.json()
    assert len(joined_data["members"]) == 2
    assert any(m["game_handle"] == "p2#IND" for m in joined_data["members"])

