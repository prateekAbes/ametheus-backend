import pytest
from app.models.game import Game
from app.models.user import User
from tests.conftest import TestSessionLocal, make_auth_headers

@pytest.mark.asyncio
async def test_organizer_tournament_crud_and_ownership(client):
    # 1. Setup game in test DB
    async with TestSessionLocal() as db:
        g = Game(
            slug="chess-test",
            name="Chess Invitational",
            category="solo",
            platform="Web Browser",
            tag="CHESS",
            is_team_game=False,
            min_team_size=1,
            max_team_size=1,
            banner_url="https://r2.ametheus.gg/chess.jpg",
            logo_url="https://r2.ametheus.gg/chess-logo.jpg",
            description="1v1 Blitz",
            server_region="India",
            anti_cheat="Anti-Engine Analysis",
            match_protocol="3+2 Blitz",
        )
        db.add(g)
        await db.commit()
        await db.refresh(g)
        g_id = g.id

    # 2. Player cannot create tournament (Rule 9)
    headers_player = make_auth_headers("usr_vortex_lead")
    payload = {
        "game_id": g_id,
        "title": "Grandmaster Blitz Cup",
        "prize_pool_inr": 10000,
        "entry_fee_inr": 0,
        "starts_at": "Tomorrow, 8 PM",
        "roster_lock_time": "Tomorrow, 7:30 PM",
        "description": "Exclusive blitz cup",
    }
    res_p = await client.post("/api/v1/organizer/tournaments", json=payload, headers=headers_player)
    assert res_p.status_code == 403

    # 3. Create users via /auth/sync and promote them
    headers_super = make_auth_headers("usr_super_admin")
    headers_org_a = make_auth_headers("usr_org_a")
    headers_org_b = make_auth_headers("usr_org_b")
    await client.post("/api/v1/auth/sync", json={"clerk_user_id": "usr_org_a", "email": "a@ametheus.gg", "username": "org_a"}, headers=headers_org_a)
    await client.post("/api/v1/auth/sync", json={"clerk_user_id": "usr_org_b", "email": "b@ametheus.gg", "username": "org_b"}, headers=headers_org_b)
    res_pa = await client.post("/api/v1/admin/users/usr_org_a/promote-organizer", headers=headers_super)
    assert res_pa.status_code == 200
    res_pb = await client.post("/api/v1/admin/users/usr_org_b/promote-organizer", headers=headers_super)
    assert res_pb.status_code == 200

    # 4. Organizer A creates tournament
    res_create = await client.post("/api/v1/organizer/tournaments", json=payload, headers=headers_org_a)
    assert res_create.status_code == 201
    tourn = res_create.json()
    assert tourn["status"] == "DRAFT"
    assert tourn["organizer_id"] == "usr_org_a"
    tourn_id = tourn["id"]

    # 5. Organizer B tries to update Organizer A's tournament -> 403 (Rule 20)
    res_b_fail = await client.put(
        f"/api/v1/organizer/tournaments/{tourn_id}",
        json={"title": "Hacked Title"},
        headers=headers_org_b,
    )
    assert res_b_fail.status_code == 403
    assert "You do not own this tournament" in res_b_fail.json()["detail"]

    # 6. Organizer A updates own tournament -> 200
    res_update = await client.put(
        f"/api/v1/organizer/tournaments/{tourn_id}",
        json={"title": "Grandmaster Blitz Cup (Season 1)"},
        headers=headers_org_a,
    )
    assert res_update.status_code == 200
    assert res_update.json()["title"] == "Grandmaster Blitz Cup (Season 1)"

    # 7. Organizer A publishes tournament -> status becomes REGISTRATION_OPEN
    res_pub = await client.post(
        f"/api/v1/organizer/tournaments/{tourn_id}/publish",
        headers=headers_org_a,
    )
    assert res_pub.status_code == 200

    # 8. Organizer A generates bracket -> status becomes LIVE
    res_bracket = await client.post(
        f"/api/v1/organizer/tournaments/{tourn_id}/generate-bracket",
        headers=headers_org_a,
    )
    assert res_bracket.status_code == 200
    assert "bracket generated" in res_bracket.json()["message"].lower()

    # 9. Query generated matches and verify match progression
    matches_res = await client.get("/api/v1/matches")
    assert matches_res.status_code == 200
    tourn_matches = [m for m in matches_res.json() if m["tournament_id"] == tourn_id]
    assert len(tourn_matches) >= 1

    first_match = tourn_matches[0]
    # Submit score
    await client.post(
        f"/api/v1/matches/{first_match['id']}/submit-result",
        json={"score_a": 13, "score_b": 5, "evidence_url": "https://r2.ametheus.gg/proof.png"},
        headers=headers_org_a,
    )

    # Super Admin or Organizer certifies result and bracket advances
    verify_res = await client.post(
        f"/api/v1/admin/matches/{first_match['id']}/verify-result",
        headers=headers_super,
    )
    assert verify_res.status_code == 200
    assert "winner advanced in bracket" in verify_res.json()["message"].lower()

    # 10. Test controlled state transitions
    # Creating tournament C
    res_c = await client.post("/api/v1/organizer/tournaments", json=payload, headers=headers_org_a)
    assert res_c.status_code == 201
    c_id = res_c.json()["id"]

    # Publish: DRAFT -> REGISTRATION_OPEN
    pub_res = await client.post(f"/api/v1/organizer/tournaments/{c_id}/publish", headers=headers_org_a)
    assert pub_res.status_code == 200

    # Close registration: REGISTRATION_OPEN -> REGISTRATION_CLOSED
    close_res = await client.post(f"/api/v1/organizer/tournaments/{c_id}/close-registration", headers=headers_org_a)
    assert close_res.status_code == 200

    # Re-open registration: REGISTRATION_CLOSED -> REGISTRATION_OPEN
    reopen_res = await client.post(f"/api/v1/organizer/tournaments/{c_id}/publish", headers=headers_org_a)
    assert reopen_res.status_code == 200

    # Cancel: REGISTRATION_OPEN -> CANCELLED
    cancel_res = await client.post(f"/api/v1/organizer/tournaments/{c_id}/cancel", headers=headers_org_a)
    assert cancel_res.status_code == 200

    # Try illegal transition: CANCELLED -> REGISTRATION_OPEN -> 400
    illegal_res = await client.post(f"/api/v1/organizer/tournaments/{c_id}/publish", headers=headers_org_a)
    assert illegal_res.status_code == 400
    assert "Illegal tournament transition" in illegal_res.json()["detail"]
