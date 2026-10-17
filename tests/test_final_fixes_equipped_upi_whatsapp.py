import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta

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
        if user.role == "SUPER_ADMIN":
            app.dependency_overrides[require_super_admin] = lambda: user
            app.dependency_overrides[require_organizer] = lambda: user
        else:
            # Let require_super_admin raise 403 for non-admins
            async def _forbidden_super_admin():
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Forbidden: Super admin required")
            app.dependency_overrides[require_super_admin] = _forbidden_super_admin
            app.dependency_overrides[require_organizer] = lambda: user

        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _create


@pytest.mark.asyncio
async def test_equipped_crest_persistence_and_clearing(mock_db, make_client):
    """
    Test 1: Profile equips a crest, persists equipped_crest_id across updates,
    and clears equipped_crest_id when switching to Google or custom upload.
    """
    player = User(
        id="usr_crest_tester",
        clerk_user_id="clerk_crest_tester",
        email="crest_tester@ametheus.gg",
        username="crest_tester",
        role="PLAYER",
        avatar_source="DEFAULT",
        avatar_url="https://example.com/default.png",
        equipped_crest_id=None,
    )
    await mock_db.users.insert_one(player.to_mongo())

    async with make_client(player) as client:
        # 1. Equip a crest by ID
        res1 = await client.put("/api/v1/auth/profile", json={
            "avatar_source": "CREST",
            "avatar_url": "data:image/svg+xml;utf8,<svg id='crest_hydra'>Hydra</svg>",
            "equipped_crest_id": "crest_hydra",
        })
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["equipped_crest_id"] == "crest_hydra"
        assert data1["avatar_source"] == "CREST"

        # Verify DB document
        db_user = await mock_db.users.find_one({"_id": "usr_crest_tester"})
        assert db_user is not None
        assert db_user["equipped_crest_id"] == "crest_hydra"
        assert db_user["avatar_source"] == "CREST"

        # 2. Switch to Google photo -> equipped_crest_id must be cleared to None
        res2 = await client.put("/api/v1/auth/profile", json={
            "avatar_source": "GOOGLE",
            "avatar_url": "https://lh3.googleusercontent.com/a/photo123",
        })
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["equipped_crest_id"] is None
        assert data2["avatar_source"] == "GOOGLE"

        db_user_2 = await mock_db.users.find_one({"_id": "usr_crest_tester"})
        assert db_user_2.get("equipped_crest_id") is None

        # 3. Equip another crest without explicit ID but matching a known crest in db.crests
        await mock_db.crests.insert_one({
            "_id": "crest_dragon_01",
            "id": "crest_dragon_01",
            "name": "Dragon Crest",
            "image_url": "https://example.com/crests/dragon.svg",
        })
        res3 = await client.put("/api/v1/auth/profile", json={
            "avatar_source": "CREST",
            "avatar_url": "https://example.com/crests/dragon.svg",
        })
        assert res3.status_code == 200
        data3 = res3.json()
        assert data3["equipped_crest_id"] == "crest_dragon_01"


@pytest.mark.asyncio
async def test_upi_account_editing_and_validation(mock_db, make_client):
    """
    Test 2: Admin can edit existing UPI payment methods via PUT and PATCH.
    Validation enforces @ in UPI and non-empty payee name. Historical code is preserved.
    Non-admin is rejected with 403 Forbidden.
    """
    admin_user = User(
        id="usr_admin",
        clerk_user_id="clerk_admin",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    regular_user = User(
        id="usr_regular",
        clerk_user_id="clerk_regular",
        email="regular@ametheus.gg",
        username="regularplayer",
        role="PLAYER",
    )
    await mock_db.users.insert_one(admin_user.to_mongo())
    await mock_db.users.insert_one(regular_user.to_mongo())

    # Insert initial UPI account
    await mock_db.upi_accounts.insert_one({
        "_id": "upi_acc_01",
        "id": "upi_acc_01",
        "code": "PRIMARY_HDFC",
        "upi_id": "ametheus@hdfcbank",
        "payee_name": "Ametheus Esports Pvt Ltd",
        "bank_name": "HDFC Bank",
        "status": "ACTIVE",
        "qr_url": "https://example.com/qr1.png",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })

    # A: Non-admin attempt to edit UPI must fail with 403
    async with make_client(regular_user) as reg_client:
        patch_unauth = await reg_client.patch("/api/v1/admin/upi-accounts/upi_acc_01", json={
            "payee_name": "Hacked Name",
        })
        assert patch_unauth.status_code == 403

    # B: Admin updates UPI via PATCH
    async with make_client(admin_user) as admin_client:
        patch_res = await admin_client.patch("/api/v1/admin/upi-accounts/upi_acc_01", json={
            "upi_id": "ametheus.official@icici",
            "payee_name": "Ametheus Gaming Ltd",
            "status": "INACTIVE",
            "qr_url": "/api/v1/media/new_qr.png",
        })
        assert patch_res.status_code == 200
        updated_data = patch_res.json()
        assert updated_data["upi_id"] == "ametheus.official@icici"
        assert updated_data["payee_name"] == "Ametheus Gaming Ltd"
        assert updated_data["status"] == "INACTIVE"
        assert updated_data["qr_url"] == "/api/v1/media/new_qr.png"
        assert updated_data["qr_code_url"] == "/api/v1/media/new_qr.png"
        assert updated_data["code"] == "PRIMARY_HDFC"  # Preserved

        # Verify in DB
        db_upi = await mock_db.upi_accounts.find_one({"_id": "upi_acc_01"})
        assert db_upi["upi_id"] == "ametheus.official@icici"
        assert db_upi["payee_name"] == "Ametheus Gaming Ltd"
        assert db_upi["status"] == "INACTIVE"
        assert db_upi["code"] == "PRIMARY_HDFC"

        # C: Admin updates via PUT
        put_res = await admin_client.put("/api/v1/admin/upi-accounts/upi_acc_01", json={
            "upi_id": "payments@oksbi",
            "payee_name": "Ametheus Tournaments",
            "bank_name": "SBI",
            "status": "ACTIVE",
        })
        assert put_res.status_code == 200
        put_data = put_res.json()
        assert put_data["upi_id"] == "payments@oksbi"
        assert put_data["payee_name"] == "Ametheus Tournaments"
        assert put_data["status"] == "ACTIVE"

        # D: Validation error on malformed UPI ID (missing @)
        bad_upi_res = await admin_client.patch("/api/v1/admin/upi-accounts/upi_acc_01", json={
            "upi_id": "invalid_upi_no_at",
        })
        assert bad_upi_res.status_code in [400, 422]

        # E: Validation error on empty payee name
        empty_payee_res = await admin_client.patch("/api/v1/admin/upi-accounts/upi_acc_01", json={
            "payee_name": "   ",
        })
        assert empty_payee_res.status_code in [400, 422]


@pytest.mark.asyncio
async def test_tournament_whatsapp_gating_lifecycle(mock_db, make_client):
    """
    Test 3: Confirmed participants cannot see/access WhatsApp link while registration is open.
    Admins can access it anytime.
    Once registration closes (capacity full, deadline passed, or status closed),
    confirmed participants can access it. Unregistered users still get 403.
    """
    player1 = User(
        id="usr_p1",
        clerk_user_id="clerk_p1",
        email="p1@ametheus.gg",
        username="player_one",
        role="PLAYER",
    )
    player_unregistered = User(
        id="usr_unreg",
        clerk_user_id="clerk_unreg",
        email="unreg@ametheus.gg",
        username="unreg_player",
        role="PLAYER",
    )
    admin_user = User(
        id="usr_admin_t",
        clerk_user_id="clerk_admin_t",
        email="admin@ametheus.gg",
        username="tournament_admin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(player1.to_mongo())
    await mock_db.users.insert_one(player_unregistered.to_mongo())
    await mock_db.users.insert_one(admin_user.to_mongo())

    future_time = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()

    # Create tournament with REGISTRATION_OPEN and capacity not full
    await mock_db.tournaments.insert_one({
        "_id": "t_whatsapp_test",
        "id": "t_whatsapp_test",
        "slug": "t-whatsapp-test",
        "title": "Championship Cup",
        "status": "REGISTRATION_OPEN",
        "entry_fee_inr": 0,
        "max_teams": 10,
        "registration_deadline": future_time,
        "whatsapp_group_url": "https://chat.whatsapp.com/SecretLeagueGroup999",
        "community_link": "https://chat.whatsapp.com/SecretLeagueGroup999",
        "community_platform": "WHATSAPP",
    })

    # Register player1 with approved status in tournament_registrations
    await mock_db.tournament_registrations.insert_one({
        "_id": "reg_p1_001",
        "id": "reg_p1_001",
        "tournament_id": "t_whatsapp_test",
        "player_id": "usr_p1",
        "user_id": "usr_p1",
        "payment_status": "FREE",
        "status": "CONFIRMED",
        "created_at": datetime.now(timezone.utc),
    })

    # SCENARIO A: Registration is OPEN -> Player 1 gets 403 Forbidden with gating message
    async with make_client(player1) as client_p1:
        res_open = await client_p1.get("/api/v1/tournaments/t-whatsapp-test/community")
        assert res_open.status_code == 403
        detail_msg = res_open.json()["detail"].lower()
        assert "registration" in detail_msg
        assert "open" in detail_msg or "unlock" in detail_msg

    # SCENARIO B: Admin can access even while registration is OPEN
    async with make_client(admin_user) as client_adm:
        res_admin = await client_adm.get("/api/v1/tournaments/t-whatsapp-test/community")
        assert res_admin.status_code == 200
        assert res_admin.json()["community_link"] == "https://chat.whatsapp.com/SecretLeagueGroup999"

    # SCENARIO C: Registration closes by admin status update
    await mock_db.tournaments.update_one(
        {"_id": "t_whatsapp_test"},
        {"$set": {"status": "REGISTRATION_CLOSED"}}
    )

    # Now Player 1 can access the WhatsApp link!
    async with make_client(player1) as client_p1:
        res_closed = await client_p1.get("/api/v1/tournaments/t-whatsapp-test/community")
        assert res_closed.status_code == 200
        cdata = res_closed.json()
        assert cdata["community_link"] == "https://chat.whatsapp.com/SecretLeagueGroup999"
        assert cdata["whatsapp_group_url"] == "https://chat.whatsapp.com/SecretLeagueGroup999"

    # SCENARIO D: Unregistered user tries to access after close -> 403 Forbidden
    async with make_client(player_unregistered) as client_unreg:
        res_unreg = await client_unreg.get("/api/v1/tournaments/t-whatsapp-test/community")
        assert res_unreg.status_code == 403
        assert "confirmed" in res_unreg.json()["detail"].lower()

    # SCENARIO E: Capacity full closure triggers unlock even if status string was still "REGISTRATION_OPEN"
    await mock_db.tournaments.update_one(
        {"_id": "t_whatsapp_test"},
        {"$set": {"status": "REGISTRATION_OPEN", "max_teams": 1}}
    )
    # Since 1 registration exists and max_teams is 1, capacity is reached
    async with make_client(player1) as client_p1:
        res_cap = await client_p1.get("/api/v1/tournaments/t-whatsapp-test/community")
        assert res_cap.status_code == 200
        assert res_cap.json()["community_link"] == "https://chat.whatsapp.com/SecretLeagueGroup999"
