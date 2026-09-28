import pytest
from tests.conftest import make_auth_headers
from app.services.email_service import send_test_email, check_smtp_health
from app.services.email_config_service import get_email_settings, get_automated_email, get_support_email, get_official_email


@pytest.mark.asyncio
async def test_public_email_settings(client, db_session):
    # 1. Public endpoint should be accessible without any auth token
    res = await client.get("/api/v1/platform/email-settings")
    assert res.status_code == 200
    data = res.json()
    assert "official_email" in data
    assert "support_email" in data
    assert "automated_email" in data
    # Operator details must never leak publicly
    assert data.get("updated_by") is None


@pytest.mark.asyncio
async def test_admin_email_settings_rbac_and_crud(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. Player cannot read admin email settings (403 Forbidden)
    res_player_get = await client.get("/api/v1/admin/email/settings", headers=player_headers)
    assert res_player_get.status_code == 403

    # 2. Player cannot update admin email settings (403 Forbidden)
    bad_update = {
        "official_email": "hack@ametheus.gg",
        "support_email": "hack@ametheus.gg",
        "automated_email": "hack@ametheus.gg",
    }
    res_player_put = await client.put("/api/v1/admin/email/settings", json=bad_update, headers=player_headers)
    assert res_player_put.status_code == 403

    # 3. Validation error: invalid email format
    invalid_payload = {
        "official_email": "not-an-email",
        "support_email": "support@ametheus.gg",
        "automated_email": "noreply@ametheus.gg",
    }
    res_invalid = await client.put("/api/v1/admin/email/settings", json=invalid_payload, headers=admin_headers)
    assert res_invalid.status_code == 422

    # 4. Super Admin gets current settings
    res_admin_get = await client.get("/api/v1/admin/email/settings", headers=admin_headers)
    assert res_admin_get.status_code == 200
    initial_data = res_admin_get.json()
    assert "@" in initial_data["official_email"]

    # 5. Super Admin updates settings
    valid_update = {
        "official_email": "official.operations@ametheus.gg",
        "support_email": "custom.support@ametheus.gg",
        "automated_email": "alerts.noreply@ametheus.gg",
    }
    res_update = await client.put("/api/v1/admin/email/settings", json=valid_update, headers=admin_headers)
    assert res_update.status_code == 200
    updated_data = res_update.json()
    assert updated_data["official_email"] == "official.operations@ametheus.gg"
    assert updated_data["support_email"] == "custom.support@ametheus.gg"
    assert updated_data["automated_email"] == "alerts.noreply@ametheus.gg"
    assert updated_data["updated_by"] is not None

    # 6. Verify database persistence in db.platform_settings
    db_doc = await db_session.platform_settings.find_one({"_id": "email_settings"})
    assert db_doc is not None
    assert db_doc["official_email"] == "official.operations@ametheus.gg"
    assert db_doc["support_email"] == "custom.support@ametheus.gg"
    assert db_doc["automated_email"] == "alerts.noreply@ametheus.gg"

    # Also verify legacy support_settings synchronization
    supp_doc = await db_session.platform_settings.find_one({"_id": "support_settings"})
    assert supp_doc is not None
    assert supp_doc["support_email"] == "custom.support@ametheus.gg"

    # 7. Verify audit log entry was created
    audit_logs = await db_session.audit_logs.find({"action": "EMAIL_SETTING_CHANGED"}).to_list(10)
    assert len(audit_logs) >= 1
    actions = [l["action"] for l in audit_logs]
    assert "EMAIL_SETTING_CHANGED" in actions

    # 8. Verify public endpoint now reflects the updated settings immediately without redeploy
    public_res = await client.get("/api/v1/platform/email-settings")
    assert public_res.status_code == 200
    pub_data = public_res.json()
    assert pub_data["official_email"] == "official.operations@ametheus.gg"
    assert pub_data["support_email"] == "custom.support@ametheus.gg"
    assert pub_data["automated_email"] == "alerts.noreply@ametheus.gg"


@pytest.mark.asyncio
async def test_smtp_diagnostics_and_test_email(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. RBAC check on smtp-status
    player_status = await client.get("/api/v1/admin/email/smtp-status", headers=player_headers)
    assert player_status.status_code == 403

    # 2. Super Admin gets SMTP status
    admin_status = await client.get("/api/v1/admin/email/smtp-status", headers=admin_headers)
    assert admin_status.status_code == 200
    st_data = admin_status.json()
    assert "configured" in st_data
    assert "connected" in st_data
    assert "authenticated" in st_data
    assert "sender" in st_data
    assert "status_code" in st_data
    # Must never expose secrets
    assert "password" not in st_data
    assert "SMTP_PASSWORD" not in st_data

    # 3. RBAC check on send-test
    player_test = await client.post("/api/v1/admin/email/send-test", json={"to_email": "test@example.com"}, headers=player_headers)
    assert player_test.status_code == 403

    # 4. Super Admin sends test email
    admin_test = await client.post(
        "/api/v1/admin/email/send-test",
        json={"to_email": "destination@example.com"},
        headers=admin_headers,
    )
    assert admin_test.status_code == 200
    test_res = admin_test.json()
    assert "success" in test_res
    assert "sender_used" in test_res
    # Passwords / secrets must never be in error details
    assert "password" not in str(test_res).lower()


@pytest.mark.asyncio
async def test_centralized_email_getters(db_session):
    # Test fallback behavior when empty vs populated
    off = await get_official_email(db_session)
    supp = await get_support_email(db_session)
    auto = await get_automated_email(db_session)

    assert "@" in off
    assert "@" in supp
    assert "@" in auto
