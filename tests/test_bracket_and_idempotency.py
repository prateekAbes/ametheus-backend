import pytest
from app.services.bracket_engine import (
    get_round_name,
    resolve_stage_format,
    build_single_elimination_bracket,
    build_double_elimination_bracket,
    build_round_robin_schedule,
    build_swiss_pairings,
    generate_tournament_bracket,
)
from app.services.wallet_service import debit_wallet_funds
from app.models.tournament import Tournament, TournamentRegistration
from app.models.team import Team, TeamMember
from app.models.user import User
from tests.conftest import mock_db

def test_round_names():
    assert get_round_name(4, 4) == "Grand Final"
    assert get_round_name(3, 4) == "Semifinals"
    assert get_round_name(2, 4) == "Quarterfinals"
    assert get_round_name(1, 4) == "Round of 16"
    assert get_round_name(1, 6) == "Round 1"

def test_stage_format_resolution():
    tourn = Tournament(
        slug="t-fmt",
        game_id="g1",
        title="Format Test",
        starts_at="Now",
        roster_lock_time="Now",
        banner_url="b",
        stage_formats={"Round 1 — Match 1": "Best of 1", "Grand Final": "Best of 5"},
        match_format="Best of 3",
    )
    assert resolve_stage_format(tourn, "Round 1 — Match 1", 1, 3) == "Best of 1"
    assert resolve_stage_format(tourn, "Grand Final", 3, 3) == "Best of 5"
    assert resolve_stage_format(tourn, "Semifinals — Match 1", 2, 3) == "Best of 3"

@pytest.mark.asyncio
async def test_bracket_generators_with_teams():
    # Setup 4 teams registered for tournament
    tourn = Tournament(
        id="tourn_bracket_test",
        slug="tourn-bracket-test",
        game_id="g_test",
        title="Bracket Engine Test",
        starts_at="TBD",
        roster_lock_time="TBD",
        banner_url="b",
        tournament_type="Single Elimination",
        match_format="Best of 3",
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    for i in range(1, 5):
        t = Team(
            id=f"team_b_{i}",
            game_id="g_test",
            captain_id=f"cap_{i}",
            name=f"Squad {i}",
            tag=f"S{i}",
        )
        await mock_db.teams.insert_one(t.to_mongo())
        reg = TournamentRegistration(
            tournament_id=tourn.id,
            team_id=t.id,
            payment_status="PAID",
        )
        await mock_db.tournament_registrations.insert_one(reg.to_mongo())

    # 1. Single Elimination
    res_single = await build_single_elimination_bracket(tourn, mock_db)
    bracket_single = res_single.get("bracket") or res_single.get("bracket_rounds", [])
    assert len(bracket_single) == 2
    assert bracket_single[0]["round_name"] == "Semifinals"
    assert bracket_single[1]["round_name"] == "Grand Final"

    # 2. Double Elimination
    res_double = await build_double_elimination_bracket(tourn, mock_db)
    bracket_double = res_double.get("bracket") or res_double.get("bracket_rounds", [])
    assert len(bracket_double) >= 3
    round_names = [r["round_name"] for r in bracket_double]
    assert any("Upper" in n for n in round_names)
    assert any("Lower" in n for n in round_names)
    assert "Grand Final" in round_names

    # 3. Round Robin
    res_rr = await build_round_robin_schedule(tourn, mock_db)
    bracket_rr = res_rr.get("bracket") or res_rr.get("bracket_rounds", [])
    assert len(bracket_rr) == 3
    total_matches = sum(len(r["matches"]) for r in bracket_rr)
    assert total_matches == 6

    # 4. Swiss Pairings
    res_swiss = await build_swiss_pairings(tourn, 1, mock_db)
    bracket_swiss = res_swiss.get("bracket") or res_swiss.get("bracket_rounds", [])
    assert len(bracket_swiss) == 1
    assert len(bracket_swiss[0]["matches"]) == 2

    # 5. Dynamic router
    tourn.tournament_type = "Double Elimination"
    res_dyn = await generate_tournament_bracket(tourn, mock_db)
    bracket_dyn = res_dyn.get("bracket") or res_dyn.get("bracket_rounds", [])
    assert any("Upper" in r["round_name"] for r in bracket_dyn)

@pytest.mark.asyncio
async def test_wallet_debit_idempotency():
    u = User(
        id="usr_idem_wallet",
        clerk_user_id="usr_idem_wallet",
        email="idem@test.com",
        username="idem_wallet_player",
        inr_balance_paise=100000,  # ₹1000
    )
    await mock_db.users.insert_one(u.to_mongo())

    # First debit of ₹250 (25000 paise)
    res1 = await debit_wallet_funds(
        db=mock_db,
        user_id=u.id,
        amount_paise=25000,
        txn_type="TOURNAMENT_ENTRY",
        reference_id="ref_idem_unique_99",
        description="Entry fee test",
    )
    assert res1 == 75000  # Returns remaining balance in paise

    # User balance must be ₹750 (75000 paise)
    user_doc = await mock_db.users.find_one({"_id": u.id})
    assert user_doc["inr_balance_paise"] == 75000

    # Second debit with EXACT SAME reference_id should be idempotent NO-OP
    res2 = await debit_wallet_funds(
        db=mock_db,
        user_id=u.id,
        amount_paise=25000,
        txn_type="TOURNAMENT_ENTRY",
        reference_id="ref_idem_unique_99",
        description="Duplicate entry fee attempt",
    )
    assert res2 == 75000  # Returns same remaining balance without deduction

    # User balance must STILL be ₹750 (NOT ₹500!)
    user_doc2 = await mock_db.users.find_one({"_id": u.id})
    assert user_doc2["inr_balance_paise"] == 75000
