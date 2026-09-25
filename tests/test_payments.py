import pytest
from app.models.tournament import Tournament
from app.models.team import Team, TeamMember
from app.models.game import Game
from app.models.user import User
from tests.conftest import TestSessionLocal, make_auth_headers

@pytest.mark.asyncio
async def test_wallet_topup_and_admin_approval(client):
    headers_user = make_auth_headers("usr_topup_01")
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u_admin = User(
            id="usr_super_admin",
            clerk_user_id="usr_super_admin",
            email="admin@ametheus.gg",
            username="admin",
            role="SUPER_ADMIN",
        )
        u1 = User(
            id="usr_topup_01",
            clerk_user_id="usr_topup_01",
            email="topup@test.com",
            username="topup_player",
            role="PLAYER",
            inr_balance_paise=0,
        )
        db.add_all([u_admin, u1])

    # 1. Initiate wallet top-up
    init_res = await client.post(
        "/api/v1/payments/wallet/topup/initiate",
        json={"amount_inr": 500.0},
        headers=headers_user,
    )
    assert init_res.status_code == 200
    init_data = init_res.json()
    assert init_data["amount_inr"] == 500.0
    assert init_data["amount_paise"] == 50000
    payment_id = init_data["payment_id"]

    # Check status is INITIATED
    status_res = await client.get(f"/api/v1/payments/{payment_id}", headers=headers_user)
    assert status_res.status_code == 200
    assert status_res.json()["status"] == "INITIATED"

    # 2. Cannot approve payment without UTR
    bad_approve = await client.post(
        f"/api/v1/admin/payments/{payment_id}/approve",
        headers=headers_admin,
    )
    assert bad_approve.status_code == 400

    # 3. Submit invalid UTR (<6 chars) -> 400
    short_utr_res = await client.post(
        f"/api/v1/payments/wallet/{payment_id}/submit-utr",
        json={"utr": "1234"},
        headers=headers_user,
    )
    assert short_utr_res.status_code == 400

    # 4. Submit valid UTR -> status becomes PENDING
    good_utr_res = await client.post(
        f"/api/v1/payments/wallet/{payment_id}/submit-utr",
        json={"utr": "UTR123456789"},
        headers=headers_user,
    )
    assert good_utr_res.status_code == 200
    assert good_utr_res.json()["status"] == "PENDING"

    # 5. Admin approves payment -> credits wallet
    approve_res = await client.post(
        f"/api/v1/admin/payments/{payment_id}/approve",
        headers=headers_admin,
    )
    assert approve_res.status_code == 200

    # Check user balance
    bal_res = await client.get("/api/v1/wallet/balance", headers=headers_user)
    assert bal_res.status_code == 200
    bal_data = bal_res.json()
    assert bal_data["available_paise"] == 50000
    assert bal_data["available_inr"] == 500.0

    # 6. Idempotent approval check (does not double credit)
    reapprove_res = await client.post(
        f"/api/v1/admin/payments/{payment_id}/approve",
        headers=headers_admin,
    )
    assert reapprove_res.status_code == 200
    bal_res2 = await client.get("/api/v1/wallet/balance", headers=headers_user)
    assert bal_res2.json()["available_paise"] == 50000

@pytest.mark.asyncio
async def test_tournament_registration_with_wallet_funds(client):
    headers_cap = make_auth_headers("usr_tourn_cap")

    async with TestSessionLocal() as db:
        u_cap = User(
            id="usr_tourn_cap",
            clerk_user_id="usr_tourn_cap",
            email="cap@tourn.test",
            username="tourn_cap",
            role="PLAYER",
            inr_balance_paise=50000,  # ₹500 in wallet
        )
        db.add(u_cap)

        g = Game(
            slug="valorant-tourn",
            name="Valorant Tourn",
            category="pc",
            platform="PC",
            tag="5V5",
            team_size=2,
            banner_url="b",
            logo_url="l",
            description="d",
        )
        db.add(g)
        await db.flush()

        t = Team(
            id="team-tourn-01",
            game_id=g.id,
            captain_id=u_cap.id,
            name="Tourn Squad",
            tag="TSQ",
            members=[
                TeamMember(user_id=u_cap.id, username="tourn_cap", role="CAPTAIN", game_handle="cap#val"),
                TeamMember(user_id="usr_member_2", username="tourn_m2", role="MEMBER", game_handle="m2#val"),
            ],
        )
        db.add(t)

        tourn = Tournament(
            id="tourn-wallet-01",
            slug="val-wallet-masters",
            game_id=g.id,
            organizer_id="usr_super_admin",
            title="Valorant Wallet Masters",
            prize_pool_inr=10000,
            entry_fee_inr=500,
            status="REGISTRATION_OPEN",
            starts_at="Next Friday",
            roster_lock_time="Next Thursday",
            banner_url="b",
            description="Wallet entry tournament",
            min_team_size=2,
            max_team_size=2,
        )
        db.add(tourn)

    # Register team for tournament using wallet balance
    reg_res = await client.post(
        f"/api/v1/tournaments/{tourn.id}/register",
        json={"team_id": t.id},
        headers=headers_cap,
    )
    assert reg_res.status_code == 200
    reg_data = reg_res.json()
    assert reg_data["payment_status"] == "PAID"

    # Verify wallet was debited
    bal_res = await client.get("/api/v1/wallet/balance", headers=headers_cap)
    assert bal_res.json()["available_paise"] == 0
    assert bal_res.json()["available_inr"] == 0.0
