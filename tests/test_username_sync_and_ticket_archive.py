import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from mongomock_motor import AsyncMongoMockClient
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import jwt

from app.main import app
from app.core.config import settings
from app.core.database import get_db
from app.api.deps import get_current_user, require_organizer
from app.models.user import User
from app.models.support import SupportTicket, SupportMessage


@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_username_and_archive_db"]


@pytest.fixture
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    pub_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    return priv_pem, pub_pem


@pytest_asyncio.fixture
async def client_with_admin(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_test_01",
        clerk_user_id="clerk_admin_01",
        email="admin@ametheus.gg",
        username="super_arbiter",
        display_name="Super Arbiter",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    await mock_db.users.insert_one(admin_user.to_mongo())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, admin_user, mock_db

    app.dependency_overrides.clear()


# =====================================================================
# PART A: USERNAME SYNCHRONIZATION AND CANONICAL MAPPING TESTS
# =====================================================================

@pytest.mark.asyncio
async def test_clerk_user_sync_new_user(mock_db, rsa_key_pair, monkeypatch):
    """Verifies that a new user syncing from Clerk receives their chosen username and Clerk ID mapping."""
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    monkeypatch.setattr(settings, "CLERK_ISSUER", "https://clerk.ametheus.gg")

    app.dependency_overrides[get_db] = lambda: mock_db
    clerk_id = "user_clerk_new_001"
    token = jwt.encode(
        {"sub": clerk_id, "iss": "https://clerk.ametheus.gg", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        priv_pem,
        algorithm="RS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "clerk_user_id": clerk_id,
            "email": "newcontender@example.com",
            "username": "ShadowSniper",
            "display_name": "Shadow Sniper",
            "has_chosen_username": True,
        }
        res = await ac.post("/api/v1/auth/sync", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["username"] == "ShadowSniper"
        assert data["clerk_user_id"] == clerk_id
        assert data["email"] == "newcontender@example.com"

        # Verify DB persistence
        doc = await mock_db.users.find_one({"clerk_user_id": clerk_id})
        assert doc is not None
        assert doc["username"] == "ShadowSniper"
        assert doc["username_lower"] == "射shadowsniper".replace("射", "")
        assert doc["username_lower"] == "shadowsniper"
        assert doc["has_chosen_username"] is True

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_clerk_user_sync_updates_placeholder_username(mock_db, rsa_key_pair, monkeypatch):
    """Verifies that when a user previously had a placeholder username, syncing from Clerk updates to their real username."""
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    monkeypatch.setattr(settings, "CLERK_ISSUER", "https://clerk.ametheus.gg")

    app.dependency_overrides[get_db] = lambda: mock_db
    clerk_id = "user_clerk_placeholder_002"

    # Pre-existing user with placeholder username
    existing_user = User(
        id="user_db_id_002",
        clerk_user_id=clerk_id,
        email="alex@ametheus.games",
        username="player_abc123",
        username_lower="player_abc123",
        has_chosen_username=False,
        needs_username=True,
    )
    await mock_db.users.insert_one(existing_user.to_mongo())

    token = jwt.encode(
        {"sub": clerk_id, "iss": "https://clerk.ametheus.gg", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        priv_pem,
        algorithm="RS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "clerk_user_id": clerk_id,
            "email": "alex@ametheus.games",
            "username": "VanguardAce",
            "display_name": "Vanguard Ace",
            "has_chosen_username": True,
        }
        res = await ac.post("/api/v1/auth/sync", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["username"] == "VanguardAce"

        # Check DB updated
        doc = await mock_db.users.find_one({"clerk_user_id": clerk_id})
        assert doc["username"] == "VanguardAce"
        assert doc["username_lower"] == "vanguardace"
        assert doc["has_chosen_username"] is True
        assert doc["needs_username"] is False

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_clerk_user_sync_handles_collision_safely(mock_db, rsa_key_pair, monkeypatch):
    """Verifies that if an incoming Clerk username collides with an existing different user, it does not overwrite the other user."""
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    monkeypatch.setattr(settings, "CLERK_ISSUER", "https://clerk.ametheus.gg")

    app.dependency_overrides[get_db] = lambda: mock_db

    # User 1 who already owns 'Phoenix'
    user_1 = User(
        id="u1_owner",
        clerk_user_id="clerk_u1",
        email="owner@example.com",
        username="Phoenix",
        username_lower="phoenix",
    )
    await mock_db.users.insert_one(user_1.to_mongo())

    # User 2 who logs in and tries to claim 'Phoenix'
    clerk_id_2 = "clerk_u2"
    token = jwt.encode(
        {"sub": clerk_id_2, "iss": "https://clerk.ametheus.gg", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        priv_pem,
        algorithm="RS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "clerk_user_id": clerk_id_2,
            "email": "user2@example.com",
            "username": "Phoenix",
        }
        res = await ac.post("/api/v1/auth/sync", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        data = res.json()
        # Should not collide with user 1: safe suffix appended
        assert data["username"].lower() != "phoenix" or data["id"] == "u1_owner"
        assert "phoenix" in data["username"].lower()

        # User 1's username remains untouched
        owner_doc = await mock_db.users.find_one({"_id": "u1_owner"})
        assert owner_doc["username"] == "Phoenix"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_user_profile_persists_canonical_username(client_with_admin):
    """Verifies that PUT /api/v1/auth/profile validates, normalizes, and persists the new canonical username."""
    ac, admin_user, mock_db = client_with_admin

    res = await ac.put("/api/v1/auth/profile", json={"username": "NeonPhantom", "display_name": "Neon Phantom"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["username"] == "NeonPhantom"
    assert data["display_name"] == "Neon Phantom"

    # Verify DB document
    doc = await mock_db.users.find_one({"_id": admin_user.id})
    assert doc["username"] == "NeonPhantom"
    assert doc["username_lower"] == "neonphantom"


# =====================================================================
# PART B: SUPPORT TICKET ARCHIVE AND RESTORATION TESTS
# =====================================================================

@pytest.mark.asyncio
async def test_archive_support_ticket_preserves_ticket_and_status(client_with_admin):
    """Verifies that archiving a support ticket sets is_archived=True, preserves original status and conversation."""
    ac, admin_user, mock_db = client_with_admin

    # Create a test ticket in IN_PROGRESS status
    ticket = SupportTicket(
        id="AMT-TEST01",
        ticket_id="AMT-TEST01",
        name="Vikram Singh",
        email="vikram@example.com",
        category="PAYMENTS",
        description="Payment verification pending for Match #402",
        status="IN_PROGRESS",
        messages=[
            SupportMessage(
                ticket_id="AMT-TEST01",
                sender_name="Vikram Singh",
                message="Here is the UTR 123456789012",
            )
        ],
    )
    await mock_db.support_tickets.insert_one(ticket.to_mongo())

    # Archive the ticket
    res = await ac.post("/api/v1/support/tickets/AMT-TEST01/archive")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["is_archived"] is True
    # CRITICAL: Status must NOT be overwritten with "ARCHIVED"
    assert data["status"] == "IN_PROGRESS"
    assert data["description"] == "Payment verification pending for Match #402"
    assert len(data["messages"]) == 1

    # Verify DB persistence
    doc = await mock_db.support_tickets.find_one({"_id": "AMT-TEST01"})
    assert doc["is_archived"] is True
    assert doc["status"] == "IN_PROGRESS"
    assert doc.get("archived_at") is not None
    assert doc.get("archived_by") == admin_user.username


@pytest.mark.asyncio
async def test_active_and_archived_ticket_queries_are_segregated(client_with_admin):
    """Verifies that GET /support/tickets filters active vs archived tickets correctly."""
    ac, admin_user, mock_db = client_with_admin

    # Clear and insert 1 active ticket and 1 archived ticket
    await mock_db.support_tickets.delete_many({})

    active_tix = SupportTicket(
        id="AMT-ACT01",
        ticket_id="AMT-ACT01",
        name="Active Player",
        email="active@example.com",
        category="TECHNICAL",
        description="Cannot connect to server",
        status="OPEN",
        is_archived=False,
    )
    archived_tix = SupportTicket(
        id="AMT-ARC01",
        ticket_id="AMT-ARC01",
        name="Archived Player",
        email="archived@example.com",
        category="ACCOUNT",
        description="Old inquiry from last month",
        status="RESOLVED",
        is_archived=True,
        archived_at=datetime.now(timezone.utc).isoformat(),
        archived_by=admin_user.username,
    )
    await mock_db.support_tickets.insert_one(active_tix.to_mongo())
    await mock_db.support_tickets.insert_one(archived_tix.to_mongo())

    # Query Active Tickets (is_archived=false)
    res_active = await ac.get("/api/v1/support/tickets?is_archived=false")
    assert res_active.status_code == 200
    active_data = res_active.json()
    assert active_data["total"] == 1
    assert active_data["tickets"][0]["ticket_id"] == "AMT-ACT01"

    # Query Archived Tickets (is_archived=true)
    res_archived = await ac.get("/api/v1/support/tickets?is_archived=true")
    assert res_archived.status_code == 200
    archived_data = res_archived.json()
    assert archived_data["total"] == 1
    assert archived_data["tickets"][0]["ticket_id"] == "AMT-ARC01"
    assert archived_data["tickets"][0]["is_archived"] is True
    assert archived_data["tickets"][0]["status"] == "RESOLVED"

    # Query Counts endpoint
    res_counts = await ac.get("/api/v1/support/tickets/counts")
    assert res_counts.status_code == 200
    counts = res_counts.json()
    assert counts["active"] == 1
    assert counts["archived"] == 1
    assert counts["total"] == 2


@pytest.mark.asyncio
async def test_unarchive_support_ticket_restores_to_active(client_with_admin):
    """Verifies that unarchiving/restoring a ticket resets is_archived=False, clears archive timestamps, and retains previous status."""
    ac, admin_user, mock_db = client_with_admin

    archived_tix = SupportTicket(
        id="AMT-RESTORE01",
        ticket_id="AMT-RESTORE01",
        name="Rohan Sharma",
        email="rohan@example.com",
        category="TOURNAMENT",
        description="Disputed match result in Quarterfinals",
        status="RESOLVED",
        is_archived=True,
        archived_at=datetime.now(timezone.utc).isoformat(),
        archived_by=admin_user.username,
    )
    await mock_db.support_tickets.insert_one(archived_tix.to_mongo())

    # Restore ticket
    res = await ac.post("/api/v1/support/tickets/AMT-RESTORE01/unarchive")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["is_archived"] is False
    assert data["status"] == "RESOLVED"  # Status must remain RESOLVED, NOT mutated to OPEN

    # Verify DB persistence
    doc = await mock_db.support_tickets.find_one({"_id": "AMT-RESTORE01"})
    assert doc["is_archived"] is False
    assert doc["status"] == "RESOLVED"
    assert doc.get("archived_at") is None
    assert doc.get("archived_by") is None
    assert doc.get("unarchived_by") == admin_user.username

    # Now verify it appears in active query again
    res_active = await ac.get("/api/v1/support/tickets?is_archived=false")
    active_ids = [t["ticket_id"] for t in res_active.json()["tickets"]]
    assert "AMT-RESTORE01" in active_ids
