import pytest
import math
from app.services.battle_royale_engine import (
    calculate_battle_royale_parameters,
    distribute_teams_into_matches,
    get_battle_royale_specs,
)

def test_pure_calculation_50_teams_bgmi():
    """
    BGMI Example from specifications:
    - 50 actual registered teams
    - Players per team = 4
    - Room capacity = 100
    - Maximum teams per match = floor(100 / 4) = 25
    - Required matches = ceil(50 / 25) = 2 matches
    Distribution:
    - Match 1: 25 teams
    - Match 2: 25 teams
    """
    calc = calculate_battle_royale_parameters(
        actual_registered_teams=50,
        room_capacity=100,
        players_per_team=4,
    )
    assert calc["max_teams_per_match"] == 25
    assert calc["required_matches"] == 2
    assert calc["room_capacity"] == 100
    assert calc["players_per_team"] == 4
    assert calc["actual_registered_teams"] == 50

    dummy_teams = [{"id": f"team_{i}", "name": f"Team {i}"} for i in range(1, 51)]
    dist = distribute_teams_into_matches(
        teams=dummy_teams,
        max_teams_per_match=calc["max_teams_per_match"],
        required_matches=calc["required_matches"],
        balanced=False,
    )
    assert len(dist) == 2
    assert len(dist[0]) == 25
    assert len(dist[1]) == 25


def test_pure_calculation_51_teams_bgmi():
    """
    BGMI Example from specifications:
    - 51 actual registered teams
    - Required matches = ceil(51 / 25) = 3 matches
    Sequential:
    - Match 1: 25 teams
    - Match 2: 25 teams
    - Match 3: 1 team
    Balanced:
    - Match 1: 17 teams
    - Match 2: 17 teams
    - Match 3: 17 teams
    """
    calc = calculate_battle_royale_parameters(
        actual_registered_teams=51,
        room_capacity=100,
        players_per_team=4,
    )
    assert calc["max_teams_per_match"] == 25
    assert calc["required_matches"] == 3

    dummy_teams = [{"id": f"team_{i}", "name": f"Team {i}"} for i in range(1, 52)]
    dist_seq = distribute_teams_into_matches(
        teams=dummy_teams,
        max_teams_per_match=calc["max_teams_per_match"],
        required_matches=calc["required_matches"],
        balanced=False,
    )
    assert len(dist_seq) == 3
    assert len(dist_seq[0]) == 25
    assert len(dist_seq[1]) == 25
    assert len(dist_seq[2]) == 1

    dist_bal = distribute_teams_into_matches(
        teams=dummy_teams,
        max_teams_per_match=calc["max_teams_per_match"],
        required_matches=calc["required_matches"],
        balanced=True,
    )
    assert len(dist_bal) == 3
    assert len(dist_bal[0]) == 17
    assert len(dist_bal[1]) == 17
    assert len(dist_bal[2]) == 17


def test_pure_calculation_free_fire():
    """
    Free Fire specifications:
    - Room capacity = 48
    - Players per team = 4
    - Maximum teams per match = floor(48 / 4) = 12
    - If 24 teams -> 2 matches of 12
    """
    room, ppt = get_battle_royale_specs("free-fire-max")
    assert room == 48
    assert ppt == 4

    calc = calculate_battle_royale_parameters(
        actual_registered_teams=24,
        room_capacity=room,
        players_per_team=ppt,
    )
    assert calc["max_teams_per_match"] == 12
    assert calc["required_matches"] == 2


@pytest.mark.asyncio
async def test_battle_royale_admin_endpoints(client, db_session):
    """
    Integration test:
    1. Seed tournament with 50 valid registered teams and 2 cancelled/rejected teams.
    2. Call GET battle-royale-calc: verifies only 50 confirmed teams counted, calculation is exact.
    3. Call POST generate-battle-royale-matches: verifies 2 matches created in database.
    4. Call POST rebalance-battle-royale-matches: moves team between lobbies.
    """
    from tests.conftest import make_auth_headers
    db = db_session
    headers = make_auth_headers("usr_super_admin")

    tourn_id = "tourn_br_test_bgmi"
    await db.tournaments.delete_many({"_id": tourn_id})
    await db.teams.delete_many({"name": {"$regex": "^BR Test Team"}})
    await db.tournament_registrations.delete_many({"tournament_id": tourn_id})
    await db.matches.delete_many({"tournament_id": tourn_id})

    # Create tournament
    tourn_doc = {
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "bgmi-br-championship",
        "title": "BGMI Battle Royale Championship",
        "game_slug": "bgmi",
        "game_id": "game_bgmi",
        "min_team_size": 4,
        "max_team_size": 4,
        "status": "REGISTRATION_CLOSED",
        "match_format": "Battle Royale",
        "format": "Battle Royale",
        "room_capacity": 100,
        "players_per_team": 4,
        "starts_at": "2026-10-01T10:00:00Z",
        "roster_lock_time": "2026-10-01T09:00:00Z",
        "banner_url": "https://example.com/banner.png",
    }
    await db.tournaments.insert_one(tourn_doc)

    # Insert 50 confirmed teams + registrations
    teams_to_insert = []
    regs_to_insert = []
    for i in range(1, 51):
        t_id = f"br_team_{i:03d}"
        teams_to_insert.append({
            "_id": t_id,
            "id": t_id,
            "name": f"BR Test Team {i:03d}",
            "tag": f"BR{i}",
            "invite_code": f"INV_BR_{i:04d}",
            "captain_id": f"cap_{i}",
            "captain_username": f"Captain_{i}",
            "members": [
                {"user_id": f"cap_{i}", "username": f"Captain_{i}", "role": "CAPTAIN"},
                {"user_id": f"player_{i}_1", "username": f"P{i}_1", "role": "PLAYER"},
                {"user_id": f"player_{i}_2", "username": f"P{i}_2", "role": "PLAYER"},
                {"user_id": f"player_{i}_3", "username": f"P{i}_3", "role": "PLAYER"},
            ],
        })
        regs_to_insert.append({
            "_id": f"reg_{i}",
            "tournament_id": tourn_id,
            "team_id": t_id,
            "payment_status": "PAID",
        })

    # Insert 2 cancelled / rejected registrations that MUST be ignored
    regs_to_insert.append({
        "_id": "reg_cancelled_99",
        "tournament_id": tourn_id,
        "team_id": "br_team_cancelled",
        "payment_status": "CANCELLED",
    })
    regs_to_insert.append({
        "_id": "reg_rejected_99",
        "tournament_id": tourn_id,
        "team_id": "br_team_rejected",
        "payment_status": "REJECTED",
    })

    await db.teams.insert_many(teams_to_insert)
    await db.tournament_registrations.insert_many(regs_to_insert)

    # 1. GET /battle-royale-calc
    resp = await client.get(
        f"/api/v1/admin/tournaments/{tourn_id}/battle-royale-calc",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["registered_teams_count"] == 50
    assert data["room_capacity"] == 100
    assert data["players_per_team"] == 4
    assert data["max_teams_per_match"] == 25
    assert data["required_matches"] == 2
    assert len(data["preview_distribution"]) == 2
    assert data["preview_distribution"][0]["team_count"] == 25
    assert data["preview_distribution"][1]["team_count"] == 25

    # 2. POST /generate-battle-royale-matches
    gen_resp = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/generate-battle-royale-matches",
        json={"stage_name": "Round 1", "round_name": "Round 1", "balanced": False},
        headers=headers,
    )
    assert gen_resp.status_code == 200, gen_resp.text
    gen_data = gen_resp.json()
    assert gen_data["status"] == "success"
    assert gen_data["total_matches"] == 2

    matches = gen_data["matches"]
    assert len(matches) == 2
    assert len(matches[0]["teams"]) == 25
    assert len(matches[1]["teams"]) == 25
    assert matches[0]["room_capacity"] == 100
    assert matches[0]["players_per_team"] == 4
    assert matches[0]["max_teams"] == 25

    # 3. POST /rebalance-battle-royale-matches (move first team from Match 1 to Match 2)
    match_1_id = matches[0]["id"]
    match_2_id = matches[1]["id"]
    team_to_move = matches[0]["teams"][0]["id"]

    rebal_resp = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rebalance-battle-royale-matches",
        json={
            "team_id": team_to_move,
            "from_match_id": match_1_id,
            "to_match_id": match_2_id,
        },
        headers=headers,
    )
    assert rebal_resp.status_code == 200, rebal_resp.text

    # Verify updated matches in DB
    m1_updated = await db.matches.find_one({"$or": [{"_id": match_1_id}, {"id": match_1_id}]})
    m2_updated = await db.matches.find_one({"$or": [{"_id": match_2_id}, {"id": match_2_id}]})
    assert len(m1_updated["teams"]) == 24
    assert len(m2_updated["teams"]) == 26
