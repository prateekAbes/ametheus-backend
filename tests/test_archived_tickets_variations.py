import pytest
from httpx import AsyncClient, ASGITransport
from mongomock_motor import AsyncMongoMockClient
from datetime import datetime, timezone

from app.main import app
from app.core.database import get_db
from app.api.deps import get_current_user, require_organizer
from app.models.user import User

@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_archived_tickets_variations_db"]

@pytest.mark.asyncio
async def test_archived_tickets_all_variations_and_views(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    admin_user = User(
        id="admin_support_01",
        email="admin@ametheus.gg",
        username="super_arbiter",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    await mock_db.users.insert_one(admin_user.to_mongo())

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Active ticket
    t_active = {
        "_id": "AMT-ACT-001",
        "ticket_id": "AMT-ACT-001",
        "name": "Active Contender",
        "email": "active@example.com",
        "category": "PAYMENT",
        "status": "OPEN",
        "description": "Inquiry about payment deposit",
        "is_archived": False,
        "created_at": now_iso,
    }

    # 2. Archived ticket variation 1: status="ARCHIVED", is_archived=False
    t_arch_status = {
        "_id": "AMT-ARC-STATUS",
        "ticket_id": "AMT-ARC-STATUS",
        "name": "Status Archived User",
        "email": "status_arch@example.com",
        "category": "TOURNAMENT",
        "status": "ARCHIVED",
        "description": "Old dispute marked archived by status",
        "is_archived": False,
        "created_at": now_iso,
    }

    # 3. Archived ticket variation 2: is_archived="true" (string)
    t_arch_string = {
        "_id": "AMT-ARC-STR",
        "ticket_id": "AMT-ARC-STR",
        "name": "String Archived User",
        "email": "string_arch@example.com",
        "category": "WALLET",
        "status": "RESOLVED",
        "description": "Old wallet question with string is_archived",
        "is_archived": "true",
        "created_at": now_iso,
    }

    # 4. Archived ticket variation 3: is_archived=True (boolean)
    t_arch_bool = {
        "_id": "AMT-ARC-BOOL",
        "ticket_id": "AMT-ARC-BOOL",
        "name": "Bool Archived User",
        "email": "bool_arch@example.com",
        "category": "ACCOUNT",
        "status": "RESOLVED",
        "description": "Old inquiry marked boolean true",
        "is_archived": True,
        "created_at": now_iso,
    }

    await mock_db.support_tickets.insert_many([t_active, t_arch_status, t_arch_string, t_arch_bool])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # A. Check ticket counts: 1 active, 3 archived
        res_counts = await ac.get("/api/v1/support/tickets/counts")
        assert res_counts.status_code == 200
        counts = res_counts.json()
        assert counts["active"] == 1
        assert counts["archived"] == 3
        assert counts["total"] == 4

        # B. Check active tickets listing: returns exactly AMT-ACT-001
        res_active = await ac.get("/api/v1/support/tickets?is_archived=false")
        assert res_active.status_code == 200
        active_data = res_active.json()
        assert active_data["total"] == 1
        assert active_data["tickets"][0]["ticket_id"] == "AMT-ACT-001"
        assert active_data["tickets"][0]["is_archived"] is False

        # C. Check archived tickets listing: returns all 3 archived tickets
        res_archived = await ac.get("/api/v1/support/tickets?is_archived=true")
        assert res_archived.status_code == 200
        arch_data = res_archived.json()
        assert arch_data["total"] == 3
        arch_ids = {t["ticket_id"] for t in arch_data["tickets"]}
        assert arch_ids == {"AMT-ARC-STATUS", "AMT-ARC-STR", "AMT-ARC-BOOL"}
        for t in arch_data["tickets"]:
            assert t["is_archived"] is True

        # D. Check grouped by contender (users-with-tickets)
        # Active view: 1 user
        res_u_active = await ac.get("/api/v1/support/users-with-tickets?is_archived=false")
        assert res_u_active.status_code == 200
        u_active = res_u_active.json()
        assert len(u_active) == 1
        assert u_active[0]["email"] == "active@example.com"

        # Archived view: 3 users
        res_u_arch = await ac.get("/api/v1/support/users-with-tickets?is_archived=true")
        assert res_u_arch.status_code == 200
        u_arch = res_u_arch.json()
        assert len(u_arch) == 3
        arch_emails = {u["email"] for u in u_arch}
        assert arch_emails == {"status_arch@example.com", "string_arch@example.com", "bool_arch@example.com"}

        # E. Unarchive / Restore a ticket
        res_restore = await ac.post("/api/v1/support/tickets/AMT-ARC-STATUS/unarchive")
        assert res_restore.status_code == 200
        restored = res_restore.json()
        assert restored["is_archived"] is False
        assert restored["status"] == "OPEN"

        # Verify counts update immediately
        res_counts_after = await ac.get("/api/v1/support/tickets/counts")
        counts_after = res_counts_after.json()
        assert counts_after["active"] == 2
        assert counts_after["archived"] == 2

    app.dependency_overrides.clear()
