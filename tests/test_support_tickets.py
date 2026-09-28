import pytest
import re
from tests.conftest import make_auth_headers


@pytest.mark.asyncio
async def test_support_ticket_lifecycle_and_audit(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. Validation error test: invalid email
    bad_payload = {
        "name": "Alex Contender",
        "email": "not-an-email",
        "category": "PAYMENT",
        "description": "My deposit didn't credit to my wallet.",
    }
    res = await client.post("/api/v1/support/tickets", json=bad_payload)
    assert res.status_code == 422

    # 2. Public ticket creation (authenticated player)
    ticket_payload = {
        "name": "Vortex Captain",
        "email": "vortex@ametheus.gg",
        "category": "PAYMENT",
        "payment_type": "UPI",
        "description": "Payment UTR submitted for BGMI qualifiers but still showing pending verification after 1 hour.",
    }
    create_res = await client.post("/api/v1/support/tickets", json=ticket_payload, headers=player_headers)
    assert create_res.status_code == 201
    created_data = create_res.json()
    ticket_id = created_data["ticket_id"]
    assert ticket_id.startswith("AMT-")
    assert re.match(r"^AMT-[0-9A-Z]{6}$", ticket_id)
    assert created_data["status"] == "OPEN"
    assert created_data["category"] == "PAYMENT"

    # 3. Verify database persistence in db.support_tickets and db.support_logs
    saved_ticket = await db_session.support_tickets.find_one({"ticket_id": ticket_id})
    assert saved_ticket is not None
    assert saved_ticket["user_id"] == "usr_vortex_lead"
    assert saved_ticket["payment_type"] == "UPI"
    assert len(saved_ticket.get("logs", [])) >= 1

    logs = await db_session.support_logs.find({"ticket_id": ticket_id}).to_list(length=10)
    assert len(logs) >= 1
    log_actions = [l["action"] for l in logs]
    assert "TICKET_CREATED" in log_actions

    # 4. Guest ticket creation (unauthenticated)
    guest_payload = {
        "name": "Guest Gamer",
        "email": "guest@gmail.com",
        "category": "TOURNAMENT",
        "description": "How do I check in for the match tonight from mobile?",
    }
    guest_res = await client.post("/api/v1/support/tickets", json=guest_payload)
    assert guest_res.status_code == 201
    guest_ticket_id = guest_res.json()["ticket_id"]
    guest_doc = await db_session.support_tickets.find_one({"ticket_id": guest_ticket_id})
    assert guest_doc["user_id"] is None

    # 5. Access control: regular player cannot list admin tickets
    player_list_res = await client.get("/api/v1/support/tickets", headers=player_headers)
    assert player_list_res.status_code == 403

    # 6. Admin ticket listing with search & filters
    admin_list_res = await client.get("/api/v1/support/tickets", headers=admin_headers)
    assert admin_list_res.status_code == 200
    list_data = admin_list_res.json()
    assert list_data["total"] >= 2
    all_ticket_ids = [t["ticket_id"] for t in list_data["tickets"]]
    assert ticket_id in all_ticket_ids

    # Search filter test
    search_res = await client.get(f"/api/v1/support/tickets?search={ticket_id}", headers=admin_headers)
    assert search_res.status_code == 200
    assert len(search_res.json()["tickets"]) == 1
    assert search_res.json()["tickets"][0]["ticket_id"] == ticket_id

    # 7. Admin get ticket details
    detail_res = await client.get(f"/api/v1/support/tickets/{ticket_id}", headers=admin_headers)
    assert detail_res.status_code == 200
    detail_data = detail_res.json()
    assert detail_data["ticket_id"] == ticket_id
    assert len(detail_data["logs"]) >= 1

    # 8. Admin status update and internal notes
    update_payload = {
        "status": "IN_PROGRESS",
        "internal_notes": "Checking bank ledger for matching UTR reference.",
    }
    update_res = await client.patch(f"/api/v1/support/tickets/{ticket_id}", json=update_payload, headers=admin_headers)
    assert update_res.status_code == 200
    updated_data = update_res.json()
    assert updated_data["status"] == "IN_PROGRESS"
    assert updated_data["internal_notes"] == "Checking bank ledger for matching UTR reference."

    # Verify audit log recorded status change & note
    after_update_logs = await db_session.support_logs.find({"ticket_id": ticket_id}).to_list(length=10)
    actions = [l["action"] for l in after_update_logs]
    assert "STATUS_CHANGED" in actions
    assert "NOTE_ADDED" in actions

    # 9. Admin resolves ticket
    resolve_res = await client.patch(
        f"/api/v1/support/tickets/{ticket_id}",
        json={"status": "RESOLVED", "internal_notes": "Payment verified and credited manually."},
        headers=admin_headers,
    )
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "RESOLVED"
    assert resolve_res.json()["resolved_at"] is not None

    # 10. Authenticated player my-tickets
    my_res = await client.get("/api/v1/support/my-tickets", headers=player_headers)
    assert my_res.status_code == 200
    my_tickets = my_res.json()
    assert any(t["ticket_id"] == ticket_id for t in my_tickets)


@pytest.mark.asyncio
async def test_support_settings_and_email_retry(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. Get default settings
    settings_res = await client.get("/api/v1/support/settings", headers=admin_headers)
    assert settings_res.status_code == 200
    assert "support_email" in settings_res.json()

    # 2. Update support email settings
    update_res = await client.put(
        "/api/v1/support/settings",
        json={"support_email": "custom-support@ametheus.gg"},
        headers=admin_headers,
    )
    assert update_res.status_code == 200
    assert update_res.json()["support_email"] == "custom-support@ametheus.gg"

    # Non-admin cannot update settings
    non_admin_res = await client.put(
        "/api/v1/support/settings",
        json={"support_email": "hacked@ametheus.gg"},
        headers=player_headers,
    )
    assert non_admin_res.status_code == 403

    # 3. Create a ticket to test retry functionality
    create_res = await client.post(
        "/api/v1/support/tickets",
        json={
            "name": "Retry Tester",
            "email": "tester@ametheus.gg",
            "category": "ACCOUNT",
            "description": "Testing email retry functionality",
        },
    )
    assert create_res.status_code == 201
    ticket_data = create_res.json()
    t_id = ticket_data["ticket_id"]
    assert "user_email" in ticket_data
    assert "support_email" in ticket_data

    # 4. Retry email endpoint - target USER
    retry_user_res = await client.post(
        f"/api/v1/support/tickets/{t_id}/retry-email",
        json={"target": "USER"},
        headers=admin_headers,
    )
    assert retry_user_res.status_code == 200
    user_retry_data = retry_user_res.json()
    assert user_retry_data["ticket_id"] == t_id
    assert "user_email_status" in user_retry_data

    # 5. Retry email endpoint - target SUPPORT
    retry_support_res = await client.post(
        f"/api/v1/support/tickets/{t_id}/retry-email",
        json={"target": "SUPPORT"},
        headers=admin_headers,
    )
    assert retry_support_res.status_code == 200
    support_retry_data = retry_support_res.json()
    assert support_retry_data["ticket_id"] == t_id
    assert "support_email_status" in support_retry_data

    # 6. Retry email endpoint - target ALL
    retry_all_res = await client.post(
        f"/api/v1/support/tickets/{t_id}/retry-email",
        json={"target": "ALL"},
        headers=admin_headers,
    )
    assert retry_all_res.status_code == 200
    all_retry_data = retry_all_res.json()
    assert all_retry_data["ticket_id"] == t_id
    assert all_retry_data["email_status"] in ["SENT", "PARTIAL", "FAILED"]

    # 7. Verify zero ticket duplication: exactly one ticket exists with this ID
    matching_tickets = await db_session.support_tickets.count_documents({"ticket_id": t_id})
    assert matching_tickets == 1

    # 8. Verify audit logs record retries
    retry_logs = await db_session.support_logs.find({
        "ticket_id": t_id,
        "action": {"$in": ["EMAIL_RETRY_USER", "EMAIL_RETRY_SUPPORT"]}
    }).to_list(10)
    assert len(retry_logs) >= 3

