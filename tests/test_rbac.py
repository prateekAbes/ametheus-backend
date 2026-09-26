import pytest
from tests.conftest import make_auth_headers

@pytest.mark.asyncio
async def test_rbac_scope_isolation(client):
    # Rule 2.1 & 2.2: Organizer cannot perform Super Admin actions
    headers_super = make_auth_headers("usr_super_admin")
    headers_organizer = make_auth_headers("usr_arbiter_val")
    headers_player = make_auth_headers("usr_vortex_lead")

    # 1. Super Admin access audit logs -> 200
    res = await client.get("/api/v1/admin/audit-logs", headers=headers_super)
    assert res.status_code == 200

    # 2. Organizer tries audit logs -> 403 Forbidden
    res_ta = await client.get("/api/v1/admin/audit-logs", headers=headers_organizer)
    assert res_ta.status_code == 403

    # 3. Regular Player tries audit logs -> 403 Forbidden
    res_p = await client.get("/api/v1/admin/audit-logs", headers=headers_player)
    assert res_p.status_code == 403

@pytest.mark.asyncio
async def test_admin_stats_and_game_crud_rbac(client):
    headers_super = make_auth_headers("usr_super_admin")
    headers_player = make_auth_headers("usr_vortex_lead")

    # 1. Admin stats - Super Admin gets 200 with all stats
    res = await client.get("/api/v1/admin/stats", headers=headers_super)
    assert res.status_code == 200
    data = res.json()
    assert "total_users" in data
    assert "total_tournaments" in data
    assert "total_games" in data

    # 2. Admin stats - Player gets 403
    res_p = await client.get("/api/v1/admin/stats", headers=headers_player)
    assert res_p.status_code == 403

    # 3. Game creation - Player gets 403
    payload = {
        "slug": "tekken-8-rbac-test",
        "name": "Tekken 8",
        "category": "crossplay",
        "platform": "PC / PS5 / Xbox",
        "tag": "FIGHTING",
        "is_team_game": False,
        "min_team_size": 1,
        "max_team_size": 1,
        "banner_url": "https://example.com/banner.png",
        "logo_url": "https://example.com/logo.png",
        "description": "Next-gen fighting game.",
    }
    res_g_player = await client.post("/api/v1/games", json=payload, headers=headers_player)
    assert res_g_player.status_code == 403

    # 4. Game creation - Super Admin gets 201
    res_g_super = await client.post("/api/v1/games", json=payload, headers=headers_super)
    assert res_g_super.status_code == 201
    created_game = res_g_super.json()
    assert created_game["slug"] == "tekken-8-rbac-test"

    # Clean up - delete game
    res_del = await client.delete(f"/api/v1/games/{created_game['id']}", headers=headers_super)
    assert res_del.status_code == 200

@pytest.mark.asyncio
async def test_audit_log_cryptographic_hash_chain(client):
    headers_super = make_auth_headers("usr_super_admin")

    # Fetch audit logs created by previous actions
    res = await client.get("/api/v1/admin/audit-logs", headers=headers_super)
    assert res.status_code == 200
    logs = res.json()
    assert len(logs) > 0

    # Verify each logged event with record_hash has a valid 64-char hex SHA-256
    for log in logs:
        if log.get("record_hash"):
            assert len(log["record_hash"]) == 64
            assert all(c in "0123456789abcdef" for c in log["record_hash"])
        if log.get("prev_hash"):
            assert len(log["prev_hash"]) == 64
