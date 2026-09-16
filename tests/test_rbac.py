import pytest

@pytest.mark.asyncio
async def test_rbac_scope_isolation(client):
    # Rule 2.1 & 2.2: Tournament Admin cannot perform Super Admin actions
    headers_super = {"x-user-id": "usr_super_admin"}
    headers_tourn_admin = {"x-user-id": "usr_arbiter_val"}
    headers_player = {"x-user-id": "usr_regular_player"}

    # 1. Super Admin access audit logs -> 200
    res = await client.get("/api/v1/admin/audit-logs", headers=headers_super)
    assert res.status_code == 200

    # 2. Tournament Admin tries audit logs -> 403 Forbidden
    res_ta = await client.get("/api/v1/admin/audit-logs", headers=headers_tourn_admin)
    assert res_ta.status_code == 403

    # 3. Regular Player tries audit logs -> 403 Forbidden
    res_p = await client.get("/api/v1/admin/audit-logs", headers=headers_player)
    assert res_p.status_code == 403
