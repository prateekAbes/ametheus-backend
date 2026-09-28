import pytest
from tests.conftest import make_auth_headers
from app.services.email_service import (
    send_support_email,
    send_noreply_email,
    send_official_email,
    get_email_system_status,
    validate_email_configuration,
    get_identity_config,
)
from app.services.email_config_service import (
    get_email_settings,
    get_automated_email,
    get_support_email,
    get_official_email,
)
from app.core.config import settings


@pytest.mark.asyncio
async def test_public_email_settings(client, db_session):
    # 1. Public endpoint should be accessible without any auth token
    res = await client.get("/api/v1/platform/email-settings")
    assert res.status_code == 200
    data = res.json()
    assert data["official_email"] == "official.ametheus@gmail.com"
    assert data["support_email"] == "support.ametheus@gmail.com"
    assert data["automated_email"] == "noreply.ametheus@gmail.com"
    # Operator details must never leak publicly
    assert data.get("updated_by") is None


@pytest.mark.asyncio
async def test_admin_email_settings_read_only_and_rbac(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. Player cannot read admin email status (403 Forbidden)
    res_player_get = await client.get("/api/v1/admin/email/status", headers=player_headers)
    assert res_player_get.status_code == 403

    # 2. Super Admin gets 3 identities status
    res_admin_status = await client.get("/api/v1/admin/email/status", headers=admin_headers)
    assert res_admin_status.status_code == 200
    st_data = res_admin_status.json()
    assert "services" in st_data
    assert "support" in st_data["services"]
    assert "noreply" in st_data["services"]
    assert "official" in st_data["services"]
    assert st_data["services"]["support"]["email"] == "support.ametheus@gmail.com"
    assert st_data["services"]["noreply"]["email"] == "noreply.ametheus@gmail.com"
    assert st_data["services"]["official"]["email"] == "official.ametheus@gmail.com"
    # Never expose passwords or raw secrets
    assert "password" not in str(st_data).lower()
    assert "app_password" not in str(st_data).lower()

    # 3. Super Admin cannot PUT to /settings (Endpoint removed)
    bad_update = {
        "official_email": "hack@ametheus.gg",
        "support_email": "hack@ametheus.gg",
        "automated_email": "hack@ametheus.gg",
    }
    res_admin_put = await client.put("/api/v1/admin/email/settings", json=bad_update, headers=admin_headers)
    assert res_admin_put.status_code in (404, 405)


@pytest.mark.asyncio
async def test_smtp_diagnostics_and_test_email(client, db_session):
    admin_headers = make_auth_headers("usr_super_admin")
    player_headers = make_auth_headers("usr_vortex_lead")

    # 1. RBAC check on smtp-status
    player_status = await client.get("/api/v1/admin/email/smtp-status?service=support", headers=player_headers)
    assert player_status.status_code == 403

    # 2. Super Admin gets SMTP status for support identity
    admin_status = await client.get("/api/v1/admin/email/smtp-status?service=support", headers=admin_headers)
    assert admin_status.status_code == 200
    st_data = admin_status.json()
    assert "configured" in st_data
    assert "connected" in st_data
    assert "authenticated" in st_data
    assert st_data["sender"] == "support.ametheus@gmail.com"
    assert "status_code" in st_data
    # Must never expose secrets
    assert "password" not in st_data
    assert "SMTP_PASSWORD" not in st_data

    # 3. RBAC check on send-test
    player_test = await client.post(
        "/api/v1/admin/email/send-test",
        json={"service": "support", "to_email": "test@example.com"},
        headers=player_headers,
    )
    assert player_test.status_code == 403

    # 4. Super Admin sends test email for noreply service
    admin_test = await client.post(
        "/api/v1/admin/email/send-test",
        json={"service": "noreply", "to_email": "destination@example.com"},
        headers=admin_headers,
    )
    assert admin_test.status_code == 200
    test_res = admin_test.json()
    assert "success" in test_res
    assert test_res["service"] == "noreply"
    assert "sender_used" in test_res
    # Passwords / secrets must never be in error details
    assert "password" not in str(test_res).lower()


@pytest.mark.asyncio
async def test_centralized_email_identities_and_validation():
    # 1. Verify identities config isolation
    supp_cfg = get_identity_config("support")
    assert supp_cfg["email"] == "support.ametheus@gmail.com"
    assert supp_cfg["host"] == "smtp.gmail.com"
    assert supp_cfg["port"] == 587
    assert supp_cfg["use_tls"] is True

    noreply_cfg = get_identity_config("noreply")
    assert noreply_cfg["email"] == "noreply.ametheus@gmail.com"
    assert noreply_cfg["host"] == "smtp.gmail.com"

    official_cfg = get_identity_config("official")
    assert official_cfg["email"] == "official.ametheus@gmail.com"
    assert official_cfg["host"] == "smtp.gmail.com"

    # 2. Validation function runs cleanly on startup
    status = validate_email_configuration()
    assert "support" in status
    assert "noreply" in status
    assert "official" in status
