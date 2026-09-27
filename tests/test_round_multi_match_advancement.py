import pytest
from tests.conftest import make_auth_headers, TestSessionLocal, mock_db
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.team import Team
from app.models.user import User

@pytest.mark.asyncio
async def test_round_multi_match_advancement_end_to_end(client):
    """
    Realistic scenario test:
    - 8 registered teams
    - Round 1 (4 matches) completed OUT OF ORDER (M3 -> M1 -> M4 -> M2)
    - Non-sequential slot filling: M3 winner fills R2 M2 Slot A while Slot B waits
    - Round 2 (2 matches) populated and completed -> advances to Round 3 (Finals)
    - Result editing updates downstream slot safely
    - Attempt to change winner after downstream match has completed raises 400 error
    - Idempotent completions
    """
    headers_admin = make_auth_headers("usr_super_admin")

    async with TestSessionLocal() as db:
        # 1. Ensure organizer user exists
        organizer = User(
            id="usr_super_admin",
            clerk_user_id="usr_super_admin",
            username="lead_organizer",
            email="organizer@ametheus.gg",
            role="SUPER_ADMIN",
            is_verified=True,
        )
        db.add(organizer)

        # 2. Create Tournament
        tourn = Tournament(
            id="tourn_advancement_cup",
            slug="championship-cup-2026",
            title="Ametheus Championship Cup 2026",
            game_id="valorant",
            starts_at="2026-10-01T10:00:00Z",
            roster_lock_time="2026-09-30T23:59:59Z",
            banner_url="https://images.unsplash.com/photo-1542751371-adc38448a05e",
            status="REGISTRATION_CLOSED",
            custom_rounds=["Round 1", "Round 2", "Grand Final"],
        )
        db.add(tourn)

        # 3. Create 8 Teams
        teams = []
        for i in range(1, 9):
            t = Team(
                id=f"team_adv_{i}",
                name=f"Team Alpha {i}",
                tag=f"A{i}",
                captain_id=f"user_{i}",
                game_id="valorant",
            )
            db.add(t)
            teams.append(t)

        # 4. Create Round 3 (Finals) match: R3 M1
        r3_m1 = Match(
            id="match_r3_m1",
            tournament_id=tourn.id,
            round_name="Grand Final",
            round_number=3,
            match_number=1,
            stage="Grand Final — Match 1",
            status="SCHEDULED",
            team_a_placeholder="Winner of Round 2 Match 1",
            team_b_placeholder="Winner of Round 2 Match 2",
        )
        db.add(r3_m1)

        # 5. Create Round 2 matches: R2 M1 and R2 M2
        r2_m1 = Match(
            id="match_r2_m1",
            tournament_id=tourn.id,
            round_name="Round 2",
            round_number=2,
            match_number=1,
            stage="Round 2 — Match 1",
            status="SCHEDULED",
            next_match_id=r3_m1.id,
            next_match_slot="A",
            team_a_placeholder="Winner of Round 1 Match 1",
            team_b_placeholder="Winner of Round 1 Match 2",
        )
        db.add(r2_m1)

        r2_m2 = Match(
            id="match_r2_m2",
            tournament_id=tourn.id,
            round_name="Round 2",
            round_number=2,
            match_number=2,
            stage="Round 2 — Match 2",
            status="SCHEDULED",
            next_match_id=r3_m1.id,
            next_match_slot="B",
            team_a_placeholder="Winner of Round 1 Match 3",
            team_b_placeholder="Winner of Round 1 Match 4",
        )
        db.add(r2_m2)

        # 6. Create Round 1 matches: 4 matches
        # M1: Team 1 vs Team 2 -> advances to R2 M1 Slot A
        r1_m1 = Match(
            id="match_r1_m1",
            tournament_id=tourn.id,
            round_name="Round 1",
            round_number=1,
            match_number=1,
            stage="Round 1 — Match 1",
            team_a_id=teams[0].id,
            team_a_name=teams[0].name,
            team_b_id=teams[1].id,
            team_b_name=teams[1].name,
            status="SCHEDULED",
            next_match_id=r2_m1.id,
            next_match_slot="A",
        )
        # M2: Team 3 vs Team 4 -> advances to R2 M1 Slot B
        r1_m2 = Match(
            id="match_r1_m2",
            tournament_id=tourn.id,
            round_name="Round 1",
            round_number=1,
            match_number=2,
            stage="Round 1 — Match 2",
            team_a_id=teams[2].id,
            team_a_name=teams[2].name,
            team_b_id=teams[3].id,
            team_b_name=teams[3].name,
            status="SCHEDULED",
            next_match_id=r2_m1.id,
            next_match_slot="B",
        )
        # M3: Team 5 vs Team 6 -> advances to R2 M2 Slot A
        r1_m3 = Match(
            id="match_r1_m3",
            tournament_id=tourn.id,
            round_name="Round 1",
            round_number=1,
            match_number=3,
            stage="Round 1 — Match 3",
            team_a_id=teams[4].id,
            team_a_name=teams[4].name,
            team_b_id=teams[5].id,
            team_b_name=teams[5].name,
            status="SCHEDULED",
            next_match_id=r2_m2.id,
            next_match_slot="A",
        )
        # M4: Team 7 vs Team 8 -> advances to R2 M2 Slot B
        r1_m4 = Match(
            id="match_r1_m4",
            tournament_id=tourn.id,
            round_name="Round 1",
            round_number=1,
            match_number=4,
            stage="Round 1 — Match 4",
            team_a_id=teams[6].id,
            team_a_name=teams[6].name,
            team_b_id=teams[7].id,
            team_b_name=teams[7].name,
            status="SCHEDULED",
            next_match_id=r2_m2.id,
            next_match_slot="B",
        )
        db.add(r1_m1)
        db.add(r1_m2)
        db.add(r1_m3)
        db.add(r1_m4)

        await db.commit()

    # 7. OUT-OF-ORDER COMPLETION:
    # Step A: Match 3 finishes FIRST (Team 5 wins 2-0 over Team 6)
    res_m3 = await client.post(
        f"/api/v1/admin/matches/{r1_m3.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[4].id, "score_a": 2, "score_b": 0},
    )
    assert res_m3.status_code == 200, res_m3.text

    # Verify R2 M2: Slot A should now have Team 5! Slot B should still be empty/placeholder.
    doc_r2_m2 = await mock_db.matches.find_one({"_id": r2_m2.id})
    assert doc_r2_m2["team_a_id"] == teams[4].id
    assert doc_r2_m2["team_a_name"] == teams[4].name
    assert doc_r2_m2.get("team_a_placeholder") is None
    assert doc_r2_m2.get("team_b_id") is None
    assert doc_r2_m2["status"] == "SCHEDULED"

    # Step B: Match 1 finishes SECOND (Team 1 wins 2-1 over Team 2)
    res_m1 = await client.post(
        f"/api/v1/admin/matches/{r1_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[0].id, "score_a": 2, "score_b": 1},
    )
    assert res_m1.status_code == 200, res_m1.text

    # Verify R2 M1: Slot A has Team 1, Slot B still empty
    doc_r2_m1 = await mock_db.matches.find_one({"_id": r2_m1.id})
    assert doc_r2_m1["team_a_id"] == teams[0].id
    assert doc_r2_m1["team_a_name"] == teams[0].name
    assert doc_r2_m1.get("team_b_id") is None

    # Step C: Match 4 finishes THIRD (Team 8 wins 2-0 over Team 7)
    res_m4 = await client.post(
        f"/api/v1/admin/matches/{r1_m4.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[7].id, "score_a": 0, "score_b": 2},
    )
    assert res_m4.status_code == 200, res_m4.text

    # Verify R2 M2: NOW BOTH SLOTS ARE FILLED! (Team 5 vs Team 8)
    doc_r2_m2 = await mock_db.matches.find_one({"_id": r2_m2.id})
    assert doc_r2_m2["team_a_id"] == teams[4].id
    assert doc_r2_m2["team_b_id"] == teams[7].id
    assert doc_r2_m2["team_b_name"] == teams[7].name
    assert doc_r2_m2.get("team_b_placeholder") is None

    # Step D: Match 2 finishes FOURTH (Team 3 wins 2-0 over Team 4)
    res_m2 = await client.post(
        f"/api/v1/admin/matches/{r1_m2.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[2].id, "score_a": 2, "score_b": 0},
    )
    assert res_m2.status_code == 200, res_m2.text

    # Verify R2 M1: BOTH SLOTS FILLED! (Team 1 vs Team 3)
    doc_r2_m1 = await mock_db.matches.find_one({"_id": r2_m1.id})
    assert doc_r2_m1["team_a_id"] == teams[0].id
    assert doc_r2_m1["team_b_id"] == teams[2].id
    assert doc_r2_m1["team_b_name"] == teams[2].name

    # 8. RESULT EDITING SUPPORT (BEFORE DOWNSTREAM MATCH STARTS):
    # Admin corrects M1 result: actually Team 2 won (1-2 instead of 2-1)!
    edit_res = await client.post(
        f"/api/v1/admin/matches/{r1_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[1].id, "score_a": 1, "score_b": 2},
    )
    assert edit_res.status_code == 200, edit_res.text

    # R2 M1 Slot A must have seamlessly switched from Team 1 to Team 2!
    doc_r2_m1 = await mock_db.matches.find_one({"_id": r2_m1.id})
    assert doc_r2_m1["team_a_id"] == teams[1].id
    assert doc_r2_m1["team_a_name"] == teams[1].name

    # Switch it back to Team 1 for tournament progression
    await client.post(
        f"/api/v1/admin/matches/{r1_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[0].id, "score_a": 2, "score_b": 1},
    )

    # 9. PLAY ROUND 2 MATCHES:
    # R2 M1: Team 1 beats Team 3 (2-1) -> advances to R3 M1 Slot A
    res_r2m1 = await client.post(
        f"/api/v1/admin/matches/{r2_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[0].id, "score_a": 2, "score_b": 1},
    )
    assert res_r2m1.status_code == 200

    # Check R3 M1 Slot A is populated with Team 1
    doc_r3 = await mock_db.matches.find_one({"_id": r3_m1.id})
    assert doc_r3["team_a_id"] == teams[0].id
    assert doc_r3["team_a_name"] == teams[0].name
    assert doc_r3.get("team_b_id") is None

    # 10. SAFETY CHECK (CORRUPTION PROTECTION):
    # R2 M1 is now COMPLETED. If someone tries to edit R1 M1 to change the winner from Team 1 to Team 2,
    # it MUST be rejected with HTTP 400!
    illegal_edit = await client.post(
        f"/api/v1/admin/matches/{r1_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[1].id, "score_a": 0, "score_b": 2},
    )
    assert illegal_edit.status_code == 400, "Should have rejected editing winner when downstream match is already completed"
    assert "downstream match" in illegal_edit.json()["detail"].lower()

    # 11. COMPLETE REMAINING MATCHES TO FINALIZE TOURNAMENT:
    # R2 M2: Team 8 beats Team 5 (2-0) -> advances to R3 M1 Slot B
    res_r2m2 = await client.post(
        f"/api/v1/admin/matches/{r2_m2.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[7].id, "score_a": 0, "score_b": 2},
    )
    assert res_r2m2.status_code == 200

    # R3 M1 now has Team 1 vs Team 8
    doc_r3 = await mock_db.matches.find_one({"_id": r3_m1.id})
    assert doc_r3["team_a_id"] == teams[0].id
    assert doc_r3["team_b_id"] == teams[7].id

    # R3 M1 completes (Team 1 wins the championship!)
    res_r3 = await client.post(
        f"/api/v1/admin/matches/{r3_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[0].id, "score_a": 3, "score_b": 1},
    )
    assert res_r3.status_code == 200

    # Tournament should now be marked COMPLETED
    final_tourn = await mock_db.tournaments.find_one({"_id": tourn.id})
    assert final_tourn["status"] == "COMPLETED"

    # 12. IDEMPOTENT EXECUTION:
    # Re-completing R3 M1 with the same winner does not crash or corrupt
    idempotent_res = await client.post(
        f"/api/v1/admin/matches/{r3_m1.id}/complete",
        headers=headers_admin,
        json={"winner_team_id": teams[0].id, "score_a": 3, "score_b": 1},
    )
    assert idempotent_res.status_code == 200
