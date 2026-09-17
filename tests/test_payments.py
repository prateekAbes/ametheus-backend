import hmac
import hashlib
import pytest
from app.core.config import settings
from app.models.tournament import Tournament, TournamentRegistration
from app.models.team import Team
from app.models.game import Game
from app.models.user import User
from tests.conftest import TestSessionLocal, make_auth_headers

@pytest.mark.asyncio
async def test_escrow_payment_order_and_verification(client):
    headers_captain = make_auth_headers("usr_pay_captain")
    headers_other = make_auth_headers("usr_pay_other")

    async with TestSessionLocal() as db:
        # Create users
        u1 = User(id="usr_pay_captain", clerk_user_id="usr_pay_captain", email="cap@pay.test", username="pay_captain", role="PLAYER")
        u2 = User(id="usr_pay_other", clerk_user_id="usr_pay_other", email="oth@pay.test", username="pay_other", role="PLAYER")
        db.add_all([u1, u2])

        # Create game
        g = Game(slug="cs2-pay", name="Counter-Strike 2", category="pc", platform="PC", tag="5V5", banner_url="b", logo_url="l", description="d")
        db.add(g)
        await db.flush()

        # Create team
        team = Team(id="team-pay-01", game_id=g.id, captain_id=u1.id, name="Payment Pros", tag="PAY")
        db.add(team)

        # Create paid tournament
        tourn = Tournament(
            id="tourn-pay-01",
            slug="cs2-pay-masters",
            game_id=g.id,
            organizer_id="usr_super_admin",
            title="CS2 Paid Masters",
            prize_pool_inr=50000,
            entry_fee_inr=500,
            status="REGISTRATION_OPEN",
            starts_at="Next Friday",
            roster_lock_time="Next Thursday",
            banner_url="b",
            description="High stakes tournament",
        )
        db.add(tourn)
        await db.commit()

    # 1. Unauthorized player tries to create escrow order -> 403
    order_payload = {"tournament_id": "tourn-pay-01", "team_id": "team-pay-01"}
    unauth_res = await client.post("/api/v1/payments/create-order", json=order_payload, headers=headers_other)
    assert unauth_res.status_code == 403

    # 2. Team Captain creates escrow order -> 200
    create_res = await client.post("/api/v1/payments/create-order", json=order_payload, headers=headers_captain)
    assert create_res.status_code == 200
    order_data = create_res.json()
    assert order_data["status"] == "created"
    assert order_data["amount_inr"] == 500
    assert order_data["amount_paise"] == 50000
    order_id = order_data["order_id"]

    # 3. Invalid signature verification -> 400
    bad_verify_payload = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": "pay_test_12345",
        "razorpay_signature": "invalid_tampered_signature",
    }
    bad_res = await client.post("/api/v1/payments/verify", json=bad_verify_payload, headers=headers_captain)
    assert bad_res.status_code == 400

    # 4. Valid HMAC signature verification -> 200
    payment_id = "pay_valid_999888"
    message = f"{order_id}|{payment_id}".encode("utf-8")
    valid_sig = hmac.new(settings.RAZORPAY_KEY_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()

    good_verify_payload = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": valid_sig,
    }
    good_res = await client.post("/api/v1/payments/verify", json=good_verify_payload, headers=headers_captain)
    assert good_res.status_code == 200
    assert "Payment verified" in good_res.json()["message"]

    # 5. Check order status endpoint
    status_res = await client.get(f"/api/v1/payments/order/{order_id}")
    assert status_res.status_code == 200
    assert status_res.json()["payment_status"] == "PAID"
