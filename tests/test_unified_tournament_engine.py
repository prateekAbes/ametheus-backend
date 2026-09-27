import pytest
from tests.conftest import make_auth_headers, TestSessionLocal, mock_db
from app.models.user import User
from app.models.tournament import Tournament
from app.models.match import Match


@pytest.mark.asyncio
async def test_tournament_format_update_preserves_matches(client):
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        t = Tournament(
            id="tourn_engine_01",
            slug="tourn-engine-01",
            game_id="valorant",
            title="CS:GO Major",
            status="REGISTRATION_CLOSED",
            starts_at="Tomorrow 6:00 PM IST",
            roster_lock_time="Tomorrow 5:00 PM IST",
            banner_url="https://img.gg/banner.png",
            format="Knockout",
            match_format="Knockout",
            series_format="BO1",
        )
        db.add(t)
        # Create a completed match
        m = Match(
            id="match_comp_01",
            tournament_id=t.id,
            stage="Quarterfinals",
            round_name="Quarterfinals",
            round_number=1,
            match_number=1,
            status="COMPLETED",
            score_a=2,
            score_b=1,
            series_format="BO3",
        )
        db.add(m)
        await db.commit()

    # Switch format mid-tournament to "Swiss System"
    res = await client.patch(
        "/api/v1/admin/tournaments/tourn_engine_01/format",
        json={"format": "Swiss System"},
        headers=headers_admin,
    )
    assert res.status_code == 200
    assert res.json()["format"] == "Swiss System"

    # Verify match still exists intact
    saved_t = await mock_db.tournaments.find_one({"$or": [{"_id": "tourn_engine_01"}, {"id": "tourn_engine_01"}]})
    assert saved_t["format"] == "Swiss System"

    saved_m = await mock_db.matches.find_one({"$or": [{"_id": "match_comp_01"}, {"id": "match_comp_01"}]})
    assert saved_m is not None
    assert saved_m["status"] == "COMPLETED"
    assert saved_m["score_a"] == 2


@pytest.mark.asyncio
async def test_admin_send_notification(client):
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        u = User(id="usr_target_player", clerk_user_id="usr_target_player", email="target@test.com", username="target_player", role="PLAYER")
        db.add(u)
        await db.commit()

    res = await client.post(
        "/api/v1/admin/notifications/send",
        json={
            "user_ids": ["usr_target_player"],
            "title": "Scheduled Match Alert",
            "message": "Your upcoming semi-final starts in 15 minutes.",
            "notification_type": "MATCH",
            "link": "/matches",
        },
        headers=headers_admin,
    )
    assert res.status_code == 200
    assert res.json()["sent_count"] == 1

    # Verify notification created in DB
    notif = await mock_db.notifications.find_one({"user_id": "usr_target_player"})
    assert notif is not None
    assert notif["title"] == "Scheduled Match Alert"
    assert notif["message"] == "Your upcoming semi-final starts in 15 minutes."


@pytest.mark.asyncio
async def test_match_creation_with_placeholders_and_bo_series(client):
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        t = Tournament(
            id="tourn_builder_01",
            slug="tourn-builder-01",
            game_id="valorant",
            title="Dota 2 Invitational",
            status="REGISTRATION_CLOSED",
            starts_at="Tomorrow 6:00 PM IST",
            roster_lock_time="Tomorrow 5:00 PM IST",
            banner_url="https://img.gg/banner.png",
        )
        db.add(t)
        await db.commit()

    # Create match with placeholders (e.g., Winner of R1M1 vs Winner of R1M2) and BO3
    res = await client.post(
        "/api/v1/admin/matches",
        json={
            "tournament_id": "tourn_builder_01",
            "team_a_placeholder": "Winner of Round 1 Match 1",
            "team_b_placeholder": "Winner of Round 1 Match 2",
            "round_name": "Semifinals",
            "round_number": 2,
            "match_number": 1,
            "series_format": "BO3",
            "scheduled_time": "Tomorrow, 4:00 PM IST",
        },
        headers=headers_admin,
    )
    assert res.status_code == 200
    match_data = res.json()["match"]
    assert match_data["round_name"] == "Semifinals"
    assert match_data["series_format"] == "BO3"
    assert match_data["team_a_placeholder"] == "Winner of Round 1 Match 1"
    assert match_data["team_b_placeholder"] == "Winner of Round 1 Match 2"


@pytest.mark.asyncio
async def test_cascade_deletion_deletes_live_matches(client):
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        t = Tournament(
            id="tourn_to_delete",
            slug="tourn-to-delete",
            game_id="valorant",
            title="Tournament For Deletion",
            status="LIVE",
            starts_at="Tomorrow 6:00 PM IST",
            roster_lock_time="Tomorrow 5:00 PM IST",
            banner_url="https://img.gg/banner.png",
        )
        db.add(t)
        m1 = Match(
            id="m_live_to_delete",
            tournament_id=t.id,
            status="LIVE",
            stage="Finals",
        )
        m2 = Match(
            id="m_sched_to_delete",
            tournament_id=t.id,
            status="SCHEDULED",
            stage="Finals",
        )
        db.add(m1)
        db.add(m2)
        await db.commit()

    # Delete the tournament via organizer endpoint
    del_res = await client.delete("/api/v1/organizer/tournaments/tourn_to_delete", headers=headers_admin)
    assert del_res.status_code == 200

    # Verify both tournament and matches are purged
    tourn_check = await mock_db.tournaments.find_one({"id": "tourn_to_delete"})
    assert tourn_check is None

    matches_check = await mock_db.matches.find({"tournament_id": "tourn_to_delete"}).to_list(length=10)
    assert len(matches_check) == 0


@pytest.mark.asyncio
async def test_upi_details_read_only_and_utr_submission(client):
    headers_player = make_auth_headers("usr_upi_player")

    async with TestSessionLocal() as db:
        u = User(id="usr_upi_player", clerk_user_id="usr_upi_player", email="upi@test.com", username="upi_player", role="PLAYER")
        db.add(u)
        t = Tournament(
            id="tourn_upi_test",
            slug="tourn-upi-test",
            game_id="valorant",
            title="UPI Tournament",
            status="REGISTRATION_OPEN",
            starts_at="Tomorrow 6:00 PM IST",
            roster_lock_time="Tomorrow 5:00 PM IST",
            banner_url="https://img.gg/banner.png",
            entry_fee_inr=100,
        )
        db.add(t)
        await db.commit()

    # 1. Fetching UPI details must NOT create any registration or payment
    upi_details_res = await client.get("/api/v1/tournaments/tourn_upi_test/upi-details", headers=headers_player)
    assert upi_details_res.status_code == 200
    upi_data = upi_details_res.json()
    assert "upi_id" in upi_data
    assert "upi_uri" in upi_data
    assert "qr_url" in upi_data

    regs = await mock_db.tournament_registrations.find({"tournament_id": "tourn_upi_test"}).to_list(length=10)
    assert len(regs) == 0
    payments = await mock_db.game_payments.find({"tournament_id": "tourn_upi_test"}).to_list(length=10)
    assert len(payments) == 0

    # 2. Submitting valid UTR creates registration and payment with PENDING_VERIFICATION
    utr_res = await client.post(
        "/api/v1/tournaments/tourn_upi_test/submit-upi-utr",
        json={"utr": "UTR_UNIFIED_999888"},
        headers=headers_player,
    )
    assert utr_res.status_code == 200
    assert utr_res.json()["payment_status"] == "PENDING_VERIFICATION"

    regs = await mock_db.tournament_registrations.find({"tournament_id": "tourn_upi_test"}).to_list(length=10)
    assert len(regs) == 1
    assert regs[0]["payment_status"] == "PENDING_VERIFICATION"
