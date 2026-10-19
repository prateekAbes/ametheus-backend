import pytest
import pytest_asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from fastapi import Header, Depends, HTTPException

from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_critical_fixes_test"]
    set_database(db)
    return db


@pytest.fixture
def make_client(mock_db):
    user_map = {}
    app.dependency_overrides[get_db] = lambda: mock_db

    async def _dynamic_current_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = authorization.replace("Bearer ", "").strip()
        if token in user_map:
            return user_map[token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _dynamic_optional_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization.replace("Bearer ", "").strip()
        return user_map.get(token)

    async def _dynamic_require_organizer(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["ORGANIZER", "SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden: Organizer required")

    async def _dynamic_require_super_admin(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden: Admin required")

    app.dependency_overrides[get_current_user] = _dynamic_current_user
    app.dependency_overrides[get_current_user_optional] = _dynamic_optional_user
    app.dependency_overrides[require_organizer] = _dynamic_require_organizer
    app.dependency_overrides[require_super_admin] = _dynamic_require_super_admin

    def _create(user: User = None):
        headers = {}
        if user:
            token = f"token_{user.id}"
            user_map[token] = user
            headers["Authorization"] = f"Bearer {token}"
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)

    return _create


@pytest.mark.asyncio
async def test_round_and_knockout_management_endpoints(mock_db, make_client):
    """Test round edit, knockout match edit, credentials save & release, and completion via endpoints."""
    admin = User(id="admin_1", username="admin_1", email="admin1@example.com", role="SUPER_ADMIN")
    client = make_client(admin)

    tourn_id = "tourn_ko_test"
    round_id = "rnd_test_1"
    match_id = "match_test_1"

    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Championship Knockout",
        "status": "IN_PROGRESS",
    })

    match_doc = {
        "_id": match_id,
        "id": match_id,
        "tournament_id": tourn_id,
        "round_name": "Round 1",
        "stage_name": "Round 1",
        "match_number": 1,
        "team_a_id": "team_1",
        "team_a_name": "Alpha",
        "team_b_id": "team_2",
        "team_b_name": "Bravo",
        "status": "SCHEDULED",
        "score_a": 0,
        "score_b": 0,
        "winner_id": None,
        "room_id": "",
        "room_password": "",
        "credentials_released": False,
    }
    await mock_db.matches.insert_one(match_doc)

    await mock_db.tournament_rounds.insert_one({
        "_id": round_id,
        "id": round_id,
        "tournament_id": tourn_id,
        "round_name": "Round 1",
        "stage_name": "Round 1",
        "format": "KNOCKOUT",
        "status": "UPCOMING",
        "is_final_round": False,
        "match_ids": [match_id],
        "matches": [match_doc],
    })

    # 1. Update round
    patch_rnd_res = await client.patch(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{round_id}",
        json={
            "round_name": "Semifinals",
            "stage_name": "Semifinals",
            "format": "KNOCKOUT",
            "is_final_round": True,
            "status": "LIVE",
        },
    )
    assert patch_rnd_res.status_code == 200
    rnd_data = patch_rnd_res.json()["round"]
    assert rnd_data["stage_name"] == "Semifinals"
    assert rnd_data["is_final_round"] is True

    # 2. Update knockout match
    patch_match_res = await client.patch(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{round_id}/matches/{match_id}",
        json={
            "match_number": 2,
            "scheduled_time": "19:30",
            "custom_instructions": "Custom lobby rules",
        },
    )
    assert patch_match_res.status_code == 200
    match_data = patch_match_res.json()["match"]
    assert match_data["match_number"] == 2
    assert match_data["scheduled_time"] == "19:30"

    # 3. Save knockout match credentials (confidential)
    save_creds_res = await client.patch(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{round_id}/matches/{match_id}/credentials",
        json={
            "room_id": "ROOM_4455",
            "room_password": "PASS_CONFIDENTIAL",
            "custom_instructions": "Do not share",
        },
    )
    assert save_creds_res.status_code == 200

    # 4. Release knockout match credentials
    rel_creds_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{round_id}/matches/{match_id}/release-credentials",
    )
    assert rel_creds_res.status_code == 200
    assert rel_creds_res.json()["status"] == "success"

    # 5. Complete knockout match
    complete_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{round_id}/matches/{match_id}/complete",
        json={
            "winner_id": "team_1",
            "score_a": 3,
            "score_b": 1,
        },
    )
    assert complete_res.status_code == 200
    assert complete_res.json()["status"] == "success"

    # Verify match state in DB
    m_db = await mock_db.matches.find_one({"id": match_id})
    assert m_db["winner_id"] == "team_1"
    assert m_db["status"] == "COMPLETED"
    assert m_db["score_a"] == 3
    assert m_db["score_b"] == 1


@pytest.mark.asyncio
async def test_unregister_participant_endpoint(mock_db, make_client):
    """Test user unregistration endpoint with entry fee refund and DB status update."""
    player = User(id="user_player_1", username="player1", email="player1@test.com", role="USER")
    client = make_client(player)

    await mock_db.users.insert_one({
        "_id": player.id,
        "id": player.id,
        "username": player.username,
        "email": player.email,
        "role": "USER",
    })

    tourn_id = "tourn_unreg_test"
    entry_fee = 250.0

    await mock_db.wallets.insert_one({
        "_id": "wallet_1",
        "user_id": player.id,
        "balance": 10000,  # 100 INR in paise
        "spending_categories": {},
    })

    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Unregister Test Cup",
        "entry_fee_inr": entry_fee,
        "status": "UPCOMING",
        "current_participants": 10,
        "max_participants": 32,
    })

    await mock_db.tournament_registrations.insert_one({
        "_id": "reg_user_1",
        "id": "reg_user_1",
        "tournament_id": tourn_id,
        "user_id": player.id,
        "player_id": player.id,
        "status": "CONFIRMED",
        "payment_status": "PAID",
        "payment_method": "WALLET",
        "amount_paid": entry_fee,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    # Call unregister endpoint
    unreg_res = await client.post(f"/api/v1/tournaments/{tourn_id}/unregister")
    assert unreg_res.status_code == 200, f"Unregister failed: {unreg_res.text}"
    body = unreg_res.json()
    assert body["status"] == "success"
    assert body["refunded_amount_inr"] == entry_fee

    # Verify registration in DB
    reg_db = await mock_db.tournament_registrations.find_one({"id": "reg_user_1"})
    assert reg_db["status"] == "CANCELLED"
    assert reg_db["payment_status"] == "CANCELLED"

    # Verify wallet refund in DB (db.users and db.wallet_ledger)
    user_db = await mock_db.users.find_one({"id": player.id})
    assert user_db["inr_balance_paise"] == 25000  # 250 INR in paise

    ledger_db = await mock_db.wallet_ledger.find_one({"user_id": player.id, "txn_type": "REFUND"})
    assert ledger_db is not None
    assert ledger_db["amount_paise"] == 25000

    # Verify capacity decremented
    tourn_db = await mock_db.tournaments.find_one({"id": tourn_id})
    assert tourn_db["current_participants"] == 9


@pytest.mark.asyncio
async def test_wallet_add_money_mandatory_screenshot_enforcement(mock_db, make_client):
    """Test submit payment UTR requires screenshot_url for WALLET_TOPUP."""
    user = User(id="user_topup", username="topup_user", email="topup@test.com", role="USER")
    client = make_client(user)

    payment_id = "pay_wallet_topup_1"
    await mock_db.game_payments.insert_one({
        "_id": payment_id,
        "id": payment_id,
        "user_id": user.id,
        "payment_type": "WALLET_TOPUP",
        "amount": 500.0,
        "status": "INITIATED",
    })

    # Missing screenshot_url -> 400 Bad Request
    res_no_screenshot = await client.post(
        f"/api/v1/payments/{payment_id}/submit-utr",
        json={
            "utr": "UTR123456789012",
            "screenshot_url": None,
        },
    )
    assert res_no_screenshot.status_code == 400
    assert "screenshot" in res_no_screenshot.json()["detail"].lower()

    # Valid screenshot_url -> 200
    res_with_screenshot = await client.post(
        f"/api/v1/payments/{payment_id}/submit-utr",
        json={
            "utr": "UTR123456789012",
            "screenshot_url": "https://res.cloudinary.com/demo/image/upload/sample.jpg",
        },
    )
    assert res_with_screenshot.status_code == 200
    assert res_with_screenshot.json()["status"] == "PENDING"

    # Verify payment in DB
    pay_db = await mock_db.game_payments.find_one({"id": payment_id})
    assert pay_db["status"] == "PENDING"
    assert pay_db["utr"] == "UTR123456789012"
    assert pay_db["screenshot_url"] == "https://res.cloudinary.com/demo/image/upload/sample.jpg"
