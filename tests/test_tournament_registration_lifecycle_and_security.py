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

@pytest.fixture
def make_client(mock_db):
    def _create(user: User):
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[require_super_admin] = lambda: user
        app.dependency_overrides[require_organizer] = lambda: user
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _create


@pytest.mark.asyncio
async def test_tournament_registration_lifecycle_end_to_end(mock_db, make_client):
    # 1. Setup seed game and tournament with capacity = 1
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
        "is_team_game": True,
        "min_team_size": 5,
        "max_team_size": 5,
    })

    await mock_db.tournaments.insert_one({
        "_id": "apex-invitational",
        "id": "apex-invitational",
        "slug": "apex-invitational",
        "title": "Apex Invitational",
        "game_id": "valorant",
        "game_slug": "valorant",
        "prize_pool_inr": 100000,
        "entry_fee_inr": 500,
        "max_teams": 1,  # Only 1 slot for concurrency/capacity test
        "min_team_size": 5,
        "max_team_size": 5,
        "status": "REGISTRATION_OPEN",
        "starts_at": "2026-12-01T14:00:00.000Z",
        "roster_lock_time": "2026-12-01T12:00:00.000Z",
        "whatsapp_group_url": "https://chat.whatsapp.com/SecretInviteGroup123",
        "community_link": "https://chat.whatsapp.com/SecretInviteGroup123",
        "community_platform": "WHATSAPP",
    })

    # Setup UPI accounts
    await mock_db.platform_upi_accounts.insert_one({
        "_id": "upi_1",
        "id": "upi_1",
        "code": "UPI001",
        "upi_id": "ametheus@okicici",
        "payee_name": "Ametheus Esports",
        "qr_url": "/api/v1/media/qr.png",
        "is_active": True,
    })

    # Users
    user1 = User(id="user_captain_1", clerk_user_id="clerk_1", email="cap1@test.gg", username="captain_alpha", role="PLAYER")
    user2 = User(id="user_captain_2", clerk_user_id="clerk_2", email="cap2@test.gg", username="captain_bravo", role="PLAYER")
    admin = User(id="admin_super", clerk_user_id="clerk_admin", email="admin@ametheus.gg", username="admin_master", role="SUPER_ADMIN")

    # Team 1
    await mock_db.teams.insert_one({
        "_id": "team_alpha",
        "id": "team_alpha",
        "name": "Team Alpha",
        "tag": "ALPHA",
        "captain_id": "user_captain_1",
        "game_id": "valorant",
        "members": [{"user_id": f"p{i}", "role": "MEMBER", "game_handle": f"p{i}"} for i in range(1, 5)],
    })

    # Team 2
    await mock_db.teams.insert_one({
        "_id": "team_bravo",
        "id": "team_bravo",
        "name": "Team Bravo",
        "tag": "BRAVO",
        "captain_id": "user_captain_2",
        "game_id": "valorant",
        "members": [{"user_id": f"b{i}", "role": "MEMBER", "game_handle": f"b{i}"} for i in range(1, 5)],
    })

    # SCENARIO A: Unregistered visitor
    async with make_client(user1) as client1:
        # Public tournament info
        t_res = await client1.get("/api/v1/tournaments/apex-invitational")
        assert t_res.status_code == 200
        t_data = t_res.json()
        assert t_data["confirmed_teams"] == 0
        assert t_data["reserved_teams"] == 0
        assert t_data["available_slots"] == 1
        assert t_data["min_team_size"] == 5
        # Private WhatsApp link is not returned in TournamentResponse
        assert t_data.get("whatsapp_group_url") is None
        assert t_data.get("community_link") is None

        # Cannot access private WhatsApp link via /community
        comm_res = await client1.get("/api/v1/tournaments/apex-invitational/community")
        assert comm_res.status_code == 403

        # SCENARIO B: Pending registration reserves slot immediately
        init_res = await client1.post("/api/v1/tournaments/apex-invitational/register/upi", json={
            "team_id": "team_alpha",
        })
        assert init_res.status_code == 200
        pay_data = init_res.json()
        payment_id = pay_data["payment_id"]
        reg_id = pay_data["registration_id"]
        assert reg_id.startswith("reg_upi_")

        # Check that slot is reserved immediately (available_slots is now 0)
        t_res2 = await client1.get("/api/v1/tournaments/apex-invitational")
        t_data2 = t_res2.json()
        assert t_data2["reserved_teams"] == 1
        assert t_data2["available_slots"] == 0

        # Status endpoint shows pending registration
        status_res = await client1.get("/api/v1/tournaments/apex-invitational/registration-status")
        assert status_res.status_code == 200
        s_data = status_res.json()
        assert s_data["is_registered"] is True
        assert s_data["registration"]["payment_status"] == "PENDING"
        assert s_data["team"]["name"] == "Team Alpha"
        assert s_data["team"]["my_role"] == "CAPTAIN"
        assert s_data["position"] is None  # Pending registration has no confirmed seed yet

        # Private WhatsApp link is still forbidden while pending
        comm_res2 = await client1.get("/api/v1/tournaments/apex-invitational/community")
        assert comm_res2.status_code == 403

    # Concurrency / Capacity: Team 2 cannot claim the reserved slot
    async with make_client(user2) as client2:
        try_res = await client2.post("/api/v1/tournaments/apex-invitational/register/upi", json={
            "team_id": "team_bravo",
        })
        assert try_res.status_code == 400
        assert "capacity" in try_res.json()["detail"].lower() or "not open" in try_res.json()["detail"].lower()

    # User 1 submits UTR
    async with make_client(user1) as client1:
        utr_res = await client1.post("/api/v1/tournaments/apex-invitational/submit-upi-utr", json={
            "payment_id": payment_id,
            "utr": "123456789012",
        })
        assert utr_res.status_code == 200

    # SCENARIO C: Admin approves payment
    async with make_client(admin) as client_admin:
        # Check admin payment list
        pay_list = await client_admin.get("/api/v1/admin/payments?status_filter=PENDING")
        assert pay_list.status_code == 200
        assert any(p["id"] == payment_id for p in pay_list.json())

        # Approve payment
        appr_res = await client_admin.post(f"/api/v1/admin/payments/{payment_id}/approve")
        assert appr_res.status_code == 200

        # Check analytics
        analytics_res = await client_admin.get("/api/v1/admin/registration-revenue-analytics")
        assert analytics_res.status_code == 200
        an_data = analytics_res.json()
        assert an_data["confirmed_registrations"] == 1
        assert an_data["approved_tournament_revenue_inr"] == 500

    # User 1 verified participant status
    async with make_client(user1) as client1:
        status_res3 = await client1.get("/api/v1/tournaments/apex-invitational/registration-status")
        assert status_res3.status_code == 200
        s_data3 = status_res3.json()
        assert s_data3["is_registered"] is True
        assert s_data3["registration"]["payment_status"] == "PAID"
        assert s_data3["position"] == 1  # Confirmed registration position is #1

        # Now authorized to view WhatsApp link
        comm_res3 = await client1.get("/api/v1/tournaments/apex-invitational/community")
        assert comm_res3.status_code == 200
        assert comm_res3.json()["community_link"] == "https://chat.whatsapp.com/SecretInviteGroup123"

    # SCENARIO D: Rejecting a pending payment releases the seat
    # Reset payment to pending for testing rejection release
    await mock_db.game_payments.update_one({"_id": payment_id}, {"$set": {"status": "PENDING"}})
    await mock_db.tournament_registrations.update_one({"_id": reg_id}, {"$set": {"payment_status": "PENDING"}})
    await mock_db.tournaments.update_one({"slug": "apex-invitational"}, {"$set": {"status": "REGISTRATION_CLOSED"}})

    async with make_client(admin) as client_admin:
        rej_res = await client_admin.post(f"/api/v1/admin/payments/{payment_id}/reject", json={
            "rejection_reason": "Invalid transaction reference",
        })
        assert rej_res.status_code == 200

        # Verify tournament reopened because capacity is now available
        t_after_rej = await mock_db.tournaments.find_one({"slug": "apex-invitational"})
        assert t_after_rej["status"] == "REGISTRATION_OPEN"

    # Team 2 can now register for the released seat
    async with make_client(user2) as client2:
        init2_res = await client2.post("/api/v1/tournaments/apex-invitational/register/upi", json={
            "team_id": "team_bravo",
        })
        assert init2_res.status_code == 200
        assert init2_res.json()["payment_id"] is not None
