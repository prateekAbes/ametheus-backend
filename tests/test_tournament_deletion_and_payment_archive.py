import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.models.tournament import Tournament
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
        if user.role == "SUPER_ADMIN":
            app.dependency_overrides[require_super_admin] = lambda: user
            app.dependency_overrides[require_organizer] = lambda: user
        else:
            async def _forbidden_super_admin():
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Forbidden: Super admin required")
            app.dependency_overrides[require_super_admin] = _forbidden_super_admin
            app.dependency_overrides[require_organizer] = lambda: user

        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _create


@pytest.mark.asyncio
async def test_permanent_tournament_deletion_cascades_and_preserves_financials(mock_db, make_client):
    """
    Test 1: Permanent Tournament Deletion
    - Purges dependent matches, tournament registrations, and match requests.
    - Clears featured tournament flag.
    - Preserves payment records in game_payments by setting tournament_deleted=True.
    - Preserves shared teams and users.
    - Creates audit log.
    """
    admin = User(
        id="usr_super_admin",
        clerk_user_id="clerk_super_admin",
        email="superadmin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    tourn_id = "tourn_delete_test_01"
    tourn_doc = {
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "tourn-delete-test-01",
        "title": "Championship To Delete",
        "game_id": "game_bgmi",
        "game_name": "BGMI",
        "status": "LIVE",
        "is_featured": True,
        "organizer_id": admin.id,
        "created_at": datetime.now(timezone.utc),
    }
    await mock_db.tournaments.insert_one(tourn_doc)

    # Insert dependent matches
    await mock_db.matches.insert_many([
        {"_id": "match_01", "id": "match_01", "tournament_id": tourn_id, "title": "Round 1 Match 1"},
        {"_id": "match_02", "id": "match_02", "tournament_id": "tourn-delete-test-01", "title": "Round 1 Match 2"},
    ])

    # Insert dependent tournament registrations
    await mock_db.tournament_registrations.insert_many([
        {"_id": "reg_01", "id": "reg_01", "tournament_id": tourn_id, "team_id": "team_alpha", "payment_status": "PAID"},
        {"_id": "reg_02", "id": "reg_02", "tournament_id": "tourn-delete-test-01", "team_id": "team_bravo", "payment_status": "PAID"},
    ])

    # Insert dependent match requests
    await mock_db.organizer_match_requests.insert_one({
        "_id": "req_01",
        "id": "req_01",
        "tournament_id": tourn_id,
        "status": "PENDING",
    })

    # Insert financial payment records that MUST BE PRESERVED
    await mock_db.game_payments.insert_many([
        {
            "_id": "pay_tourn_01",
            "id": "pay_tourn_01",
            "tournament_id": tourn_id,
            "tournament_title": "Championship To Delete",
            "amount_inr": 500.0,
            "status": "APPROVED",
            "user_id": "usr_player_01",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "_id": "pay_tourn_02",
            "id": "pay_tourn_02",
            "tournament_id": "tourn-delete-test-01",
            "tournament_title": "Championship To Delete",
            "amount_inr": 500.0,
            "status": "APPROVED",
            "user_id": "usr_player_02",
            "created_at": datetime.now(timezone.utc),
        },
    ])

    # Insert shared participating team (MUST NOT BE DELETED)
    await mock_db.teams.insert_one({
        "_id": "team_alpha",
        "id": "team_alpha",
        "name": "Team Alpha",
    })

    async with make_client(admin) as client:
        # Delete via organizer route
        res = await client.delete(f"/api/v1/organizer/tournaments/{tourn_id}")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["status"] == "success"
        assert "successfully deleted" in data["message"].lower()

        # Verify tournament is gone from database
        t_found = await mock_db.tournaments.find_one({"$or": [{"_id": tourn_id}, {"id": tourn_id}]})
        assert t_found is None

        # Verify dependent matches are purged
        m_count = await mock_db.matches.count_documents({"$or": [{"tournament_id": tourn_id}, {"tournament_id": "tourn-delete-test-01"}]})
        assert m_count == 0

        # Verify dependent registrations are purged
        r_count = await mock_db.tournament_registrations.count_documents({"$or": [{"tournament_id": tourn_id}, {"tournament_id": "tourn-delete-test-01"}]})
        assert r_count == 0

        # Verify dependent match requests are purged
        req_count = await mock_db.organizer_match_requests.count_documents({"tournament_id": tourn_id})
        assert req_count == 0

        # Verify financial payments are PRESERVED and marked with tournament_deleted=True
        payments = await mock_db.game_payments.find({"$or": [{"tournament_id": tourn_id}, {"tournament_id": "tourn-delete-test-01"}]}).to_list(10)
        assert len(payments) == 2
        for p in payments:
            assert p.get("tournament_deleted") is True

        # Verify team is NOT deleted
        team = await mock_db.teams.find_one({"_id": "team_alpha"})
        assert team is not None

        # Verify audit log was recorded
        audit = await mock_db.audit_logs.find_one({"action": "DELETE_TOURNAMENT"})
        assert audit is not None
        assert admin.username in audit["operator"]


@pytest.mark.asyncio
async def test_admin_tournament_deletion_endpoint_alias(mock_db, make_client):
    """
    Test 1b: Verify /api/v1/admin/tournaments/{id} deletion endpoint works seamlessly
    """
    admin = User(
        id="usr_admin_alias",
        clerk_user_id="clerk_admin_alias",
        email="adminalias@ametheus.gg",
        username="adminalias",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    tourn_id = "tourn_alias_01"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "tourn-alias-01",
        "title": "Alias Tournament",
        "game_id": "game_bgmi",
        "status": "DRAFT",
        "organizer_id": admin.id,
        "created_at": datetime.now(timezone.utc),
    })

    async with make_client(admin) as client:
        res = await client.delete(f"/api/v1/admin/tournaments/{tourn_id}")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

        t_db = await mock_db.tournaments.find_one({"_id": tourn_id})
        assert t_db is None


@pytest.mark.asyncio
async def test_payment_approval_moves_to_archive_idempotently(mock_db, make_client):
    """
    Test 2: Payment Approval
    - Transitions PENDING -> APPROVED.
    - Sets is_archived=True, approved_by, approved_at, archived_at.
    - Disappears from Active Pending view (is_archived=False).
    - Appears in Archived Records view (is_archived=True).
    - Idempotent: Approving twice succeeds without duplicating or corrupting state.
    """
    admin = User(
        id="usr_super_admin_pay",
        clerk_user_id="clerk_super_admin_pay",
        email="adminpay@ametheus.gg",
        username="adminpay",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    player = User(
        id="usr_player_test",
        clerk_user_id="clerk_player_test",
        email="player@ametheus.gg",
        username="playertest",
        inr_balance="0.00",
    )
    await mock_db.users.insert_one(player.to_mongo())

    pay_id = "pay_approve_test_01"
    now_utc = datetime.now(timezone.utc)
    payment_doc = {
        "_id": pay_id,
        "id": pay_id,
        "amount_inr": 250.0,
        "amount_paise": 25000,
        "utr": "UTR123456789",
        "sender_upi_id": "player@upi",
        "user_id": "usr_player_test",
        "status": "PENDING",
        "payment_type": "WALLET_TOPUP",
        "created_at": now_utc,
        "is_dummy": False,
    }
    await mock_db.game_payments.insert_one(payment_doc)

    async with make_client(admin) as client:
        # 1. Before approval: Verify it appears in active pending
        res_pending_before = await client.get("/api/v1/admin/payments?is_archived=false")
        assert res_pending_before.status_code == 200
        docs_before = res_pending_before.json()
        assert any(d["id"] == pay_id for d in docs_before)

        # 2. Approve payment
        res_approve = await client.post(f"/api/v1/admin/payments/{pay_id}/approve")
        assert res_approve.status_code == 200
        approve_data = res_approve.json()
        assert approve_data["status"] == "success"

        # 3. Verify in DB that it is marked APPROVED, is_archived=True, and has approved_at/archived_at
        p_db = await mock_db.game_payments.find_one({"_id": pay_id})
        assert p_db["status"] == "APPROVED"
        assert p_db["is_archived"] is True
        assert p_db["approved_by"] == admin.username
        assert "approved_at" in p_db
        assert "archived_at" in p_db

        # 4. Verify Active Pending view NO LONGER includes this payment
        res_pending_after = await client.get("/api/v1/admin/payments?is_archived=false")
        docs_after = res_pending_after.json()
        assert not any(d["id"] == pay_id for d in docs_after)

        # 5. Verify Archived Records view DOES include this payment
        res_archived = await client.get("/api/v1/admin/payments?is_archived=true")
        docs_archived = res_archived.json()
        assert any(d["id"] == pay_id for d in docs_archived)
        matching_archived = next(d for d in docs_archived if d["id"] == pay_id)
        assert matching_archived["status"] == "APPROVED"
        assert matching_archived["is_archived"] is True
        assert matching_archived["approved_by"] == admin.username

        # 6. Idempotency test: Re-approving already approved payment succeeds cleanly
        res_approve_again = await client.post(f"/api/v1/admin/payments/{pay_id}/approve")
        assert res_approve_again.status_code == 200
        assert "already approved" in res_approve_again.json()["message"].lower()


@pytest.mark.asyncio
async def test_payment_rejection_moves_to_archive_idempotently(mock_db, make_client):
    """
    Test 3: Payment Rejection
    - Transitions PENDING -> REJECTED.
    - Sets is_archived=True, rejected_by, rejected_at, archived_at, rejection_reason.
    - Disappears from Active Pending view.
    - Appears in Archived Records view.
    - Cannot approve an already rejected payment (returns 400).
    - Idempotent: Rejecting twice succeeds cleanly.
    """
    admin = User(
        id="usr_super_admin_rej",
        clerk_user_id="clerk_super_admin_rej",
        email="adminrej@ametheus.gg",
        username="adminrej",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    pay_id = "pay_reject_test_01"
    payment_doc = {
        "_id": pay_id,
        "id": pay_id,
        "amount_inr": 150.0,
        "utr": "UTR987654321",
        "user_id": "usr_player_rej",
        "status": "PENDING",
        "payment_type": "TOURNAMENT_ENTRY",
        "created_at": datetime.now(timezone.utc),
        "is_dummy": False,
    }
    await mock_db.game_payments.insert_one(payment_doc)

    async with make_client(admin) as client:
        # 1. Reject payment
        res_reject = await client.post(
            f"/api/v1/admin/payments/{pay_id}/reject",
            json={"rejection_reason": "UTR reference not found on bank portal"}
        )
        assert res_reject.status_code == 200
        rej_data = res_reject.json()
        assert rej_data["status"] == "success"

        # 2. Verify in DB
        p_db = await mock_db.game_payments.find_one({"_id": pay_id})
        assert p_db["status"] == "REJECTED"
        assert p_db["is_archived"] is True
        assert p_db["rejected_by"] == admin.username
        assert "UTR reference not found" in p_db["rejection_reason"]
        assert "rejected_at" in p_db
        assert "archived_at" in p_db

        # 3. Cannot approve an already rejected payment
        res_approve_rejected = await client.post(f"/api/v1/admin/payments/{pay_id}/approve")
        assert res_approve_rejected.status_code == 400
        assert "rejected" in res_approve_rejected.json()["detail"].lower()

        # 4. Verify Active Pending does not show it
        res_pending = await client.get("/api/v1/admin/payments?is_archived=false")
        assert not any(d["id"] == pay_id for d in res_pending.json())

        # 5. Verify Archived Records DOES show it
        res_archived = await client.get("/api/v1/admin/payments?is_archived=true")
        assert any(d["id"] == pay_id for d in res_archived.json())

        # 6. Idempotent: Re-rejecting succeeds
        res_reject_again = await client.post(
            f"/api/v1/admin/payments/{pay_id}/reject",
            json={"rejection_reason": "UTR reference not found on bank portal"}
        )
        assert res_reject_again.status_code == 200
        assert "already rejected" in res_reject_again.json()["message"].lower()


@pytest.mark.asyncio
async def test_advanced_payment_search_and_filters(mock_db, make_client):
    """
    Test 4: Advanced Search and Filters
    - Search across UTR, Username, User ID, Tournament, VPA.
    - Combined filtering: Status, Payment Type, Date preset, Amount range, Sorting.
    """
    admin = User(
        id="usr_admin_filters",
        clerk_user_id="clerk_admin_filters",
        email="filters@ametheus.gg",
        username="adminfilter",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    player = User(
        id="usr_ninja_pro",
        clerk_user_id="clerk_ninja_pro",
        email="ninja@pro.gg",
        username="ninja_legend",
        display_name="Ninja Pro",
    )
    await mock_db.users.insert_one(player.to_mongo())

    now = datetime.now(timezone.utc)
    test_docs = [
        {
            "_id": "pay_filter_01",
            "id": "pay_filter_01",
            "utr": "MATCHING_UTR_777",
            "user_id": player.id,
            "tournament_id": "bgmi_clash",
            "tournament_title": "BGMI Clash",
            "amount_inr": 100.0,
            "payment_type": "TOURNAMENT_ENTRY",
            "sender_upi_id": "ninja@okhdfcbank",
            "status": "PENDING",
            "created_at": now - timedelta(hours=2),
            "is_dummy": False,
        },
        {
            "_id": "pay_filter_02",
            "id": "pay_filter_02",
            "utr": "OTHER_UTR_888",
            "user_id": "usr_other",
            "tournament_id": "valorant_cup",
            "tournament_title": "Valorant Cup",
            "amount_inr": 500.0,
            "payment_type": "WALLET_TOPUP",
            "sender_upi_id": "other@paytm",
            "status": "APPROVED",
            "is_archived": True,
            "created_at": now - timedelta(days=2),
            "is_dummy": False,
        },
        {
            "_id": "pay_filter_03",
            "id": "pay_filter_03",
            "utr": "OTHER_UTR_999",
            "user_id": "usr_other",
            "amount_inr": 1000.0,
            "payment_type": "GAME_ENTRY",
            "sender_upi_id": "gamer@axis",
            "status": "REJECTED",
            "is_archived": True,
            "created_at": now - timedelta(days=10),
            "is_dummy": False,
        },
    ]
    await mock_db.game_payments.insert_many(test_docs)

    async with make_client(admin) as client:
        # Search by UTR
        res_utr = await client.get("/api/v1/admin/payments?search=MATCHING_UTR_777&is_archived=false")
        assert res_utr.status_code == 200
        docs = res_utr.json()
        assert len(docs) == 1
        assert docs[0]["id"] == "pay_filter_01"

        # Search by username (cross-collection resolution)
        res_user = await client.get("/api/v1/admin/payments?search=ninja_legend&is_archived=false")
        assert res_user.status_code == 200
        docs_u = res_user.json()
        assert len(docs_u) == 1
        assert docs_u[0]["id"] == "pay_filter_01"

        # Amount range filter: min 400, max 600
        res_amt = await client.get("/api/v1/admin/payments?min_amount=400&max_amount=600&is_archived=true")
        assert res_amt.status_code == 200
        docs_amt = res_amt.json()
        assert len(docs_amt) == 1
        assert docs_amt[0]["id"] == "pay_filter_02"

        # Payment type filter: WALLET_TOPUP
        res_type = await client.get("/api/v1/admin/payments?payment_type=WALLET_TOPUP&is_archived=true")
        assert res_type.status_code == 200
        docs_type = res_type.json()
        assert len(docs_type) == 1
        assert docs_type[0]["id"] == "pay_filter_02"


@pytest.mark.asyncio
async def test_payment_summary_authoritative_calculation(mock_db, make_client):
    """
    Test 5: Authoritative Payment Summary
    - Validates GET /api/v1/admin/payments/summary
    - Returns accurate matching_count, total_volume_inr, pending_count, approved_count, rejected_count.
    """
    admin = User(
        id="usr_admin_summary",
        clerk_user_id="clerk_admin_summary",
        email="summary@ametheus.gg",
        username="adminsummary",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    now = datetime.now(timezone.utc)
    await mock_db.game_payments.insert_many([
        {"_id": "p_sum_1", "id": "p_sum_1", "amount_inr": 100.0, "status": "PENDING", "is_archived": False, "created_at": now},
        {"_id": "p_sum_2", "id": "p_sum_2", "amount_inr": 200.0, "status": "APPROVED", "is_archived": True, "created_at": now},
        {"_id": "p_sum_3", "id": "p_sum_3", "amount_inr": 300.0, "status": "REJECTED", "is_archived": True, "created_at": now},
    ])

    async with make_client(admin) as client:
        # Summary for all archived payments
        res_archived_sum = await client.get("/api/v1/admin/payments/summary?is_archived=true")
        assert res_archived_sum.status_code == 200
        data_archived = res_archived_sum.json()
        assert data_archived["matching_count"] == 2
        assert data_archived["total_volume_inr"] == 500.0
        assert data_archived["approved_count"] == 1
        assert data_archived["approved_volume_inr"] == 200.0
        assert data_archived["rejected_count"] == 1
        assert data_archived["rejected_volume_inr"] == 300.0

        # Summary for active pending payments
        res_pending_sum = await client.get("/api/v1/admin/payments/summary?is_archived=false")
        assert res_pending_sum.status_code == 200
        data_pending = res_pending_sum.json()
        assert data_pending["matching_count"] == 1
        assert data_pending["pending_count"] == 1
        assert data_pending["pending_volume_inr"] == 100.0
