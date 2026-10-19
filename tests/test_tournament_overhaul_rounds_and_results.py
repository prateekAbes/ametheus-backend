import pytest
import pytest_asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from fastapi import Header, Depends, HTTPException

from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_tournament_overhaul_test"]
    set_database(db)
    return db


@pytest.fixture
def make_client(mock_db):
    user_map = {}
    app.dependency_overrides[get_db] = lambda: mock_db

    async def _dynamic_current_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = authorization.replace("Bearer ", "").strip()
        if token in user_map:
            return user_map[token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _dynamic_optional_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization.replace("Bearer ", "").strip()
        return user_map.get(token)

    async def _dynamic_require_organizer(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["ORGANIZER", "SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden: Organizer required")

    async def _dynamic_require_super_admin(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden: Admin required")

    app.dependency_overrides[get_current_user] = _dynamic_current_user
    app.dependency_overrides[get_current_user_optional] = _dynamic_optional_user
    app.dependency_overrides[require_organizer] = _dynamic_require_organizer
    app.dependency_overrides[require_super_admin] = _dynamic_require_super_admin

    def _create(user: User = None):
        headers = {}
        if user:
            token = f"token_{user.id}"
            user_map[token] = user
            headers["Authorization"] = f"Bearer {token}"
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)

    return _create


@pytest.mark.asyncio
async def test_tournament_creation_with_mvp_and_prize_distribution(mock_db, make_client):
    admin = User(id="admin_1", username="admin_1", email="admin1@example.com", role="SUPER_ADMIN")
    client = make_client(admin)

    # Insert game with supported modes
    await mock_db.games.insert_one({
        "_id": "bgmi",
        "id": "bgmi",
        "slug": "bgmi",
        "name": "Battlegrounds Mobile India",
        "supported_game_modes": ["SOLO", "DUO", "SQUAD"],
        "category": "Battle Royale",
        "is_team_game": True,
    })

    # Create tournament with enable_mvp and prize_distribution
    create_payload = {
        "game_id": "bgmi",
        "title": "Ametheus Pro Series",
        "prize_pool_inr": 50000,
        "entry_fee_inr": 100,
        "participation_mode": "SQUAD",
        "max_teams": 16,
        "format": "Battle Royale",
        "enable_mvp": True,
        "prize_distribution": [
            {"name": "Champions", "amount": 30000},
            {"name": "Runners Up", "amount": 15000},
            {"name": "Tournament MVP", "amount": 5000},
        ],
        "description": "Premier esports championship",
        "starts_at": "2026-11-01T18:00:00Z",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=create_payload)
    assert res.status_code == 201
    tourn_data = res.json()
    assert tourn_data["enable_mvp"] is True
    assert len(tourn_data["prize_distribution"]) == 3
    assert tourn_data["prize_distribution"][0]["name"] == "Champions"

    tourn_id = tourn_data["id"]

    # Public endpoint check
    pub_res = await client.get(f"/api/v1/tournaments/{tourn_id}")
    assert pub_res.status_code == 200
    assert pub_res.json()["enable_mvp"] is True
    assert len(pub_res.json()["prize_distribution"]) == 3


@pytest.mark.asyncio
async def test_manual_round_creation_and_battle_royale_progression(mock_db, make_client):
    admin = User(id="admin_1", username="admin_1", email="admin1@example.com", role="SUPER_ADMIN")
    client = make_client(admin)

    tourn_id = "t_br_test"
    # Seed tournament
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": tourn_id,
        "title": "BR Championship",
        "game_id": "bgmi",
        "participation_mode": "SQUAD",
        "status": "REGISTRATION_CLOSED",
        "enable_mvp": True,
        "format": "Battle Royale",
    })

    # Seed 4 registered teams
    teams = [
        {"id": f"team_{i}", "name": f"Team Alpha {i}", "tag": f"A{i}", "captain_id": f"cap_{i}"}
        for i in range(1, 5)
    ]
    for tm in teams:
        await mock_db.teams.insert_one({
            "_id": tm["id"],
            "id": tm["id"],
            "name": tm["name"],
            "tag": tm["tag"],
            "captain_id": tm["captain_id"],
            "members": [{"user_id": tm["captain_id"]}],
        })
        await mock_db.tournament_registrations.insert_one({
            "_id": f"reg_{tm['id']}",
            "tournament_id": tourn_id,
            "team_id": tm["id"],
            "payment_status": "PAID",
        })

    # 1. Create Round 1 with strictly 3 fields
    r1_payload = {
        "stage_name": "Round 1",
        "competition_format": "Battle Royale",
        "is_final_round": False,
    }
    r1_res = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds", json=r1_payload)
    assert r1_res.status_code == 200
    r1_data = r1_res.json()["round"]
    assert r1_data["round_number"] == 1
    assert r1_data["format"] == "BATTLE_ROYALE"
    assert len(r1_data["eligible_contenders"]) == 4

    r1_id = r1_data["id"]

    # 2. Save Credentials (Room ID and optional Room Password)
    cred_save = await client.patch(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r1_id}/credentials",
        json={"room_id": "ROOM_777", "room_password": "PASS_ABC"}
    )
    assert cred_save.status_code == 200
    assert cred_save.json()["credentials_released"] is False

    # 3. Release / Send Credentials
    cred_send = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r1_id}/release-credentials")
    assert cred_send.status_code == 200
    assert cred_send.json()["credentials_released"] is True

    # 4. Start Round Live
    live_res = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r1_id}/go-live")
    assert live_res.status_code == 200
    assert live_res.json()["round_status"] == "LIVE"

    # 5. Save Qualifiers: Team 1 and Team 2 advance; Team 3 and 4 eliminated
    qual_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r1_id}/qualifiers",
        json={"qualifier_ids": ["team_1", "team_2"]}
    )
    assert qual_res.status_code == 200
    assert set(qual_res.json()["qualifier_ids"]) == {"team_1", "team_2"}
    assert set(qual_res.json()["eliminated_ids"]) == {"team_3", "team_4"}

    # 6. Save Round MVP
    mvp_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r1_id}/mvp",
        json={"player_id": "cap_1", "player_name": "SniperKing", "stats": "8 kills"}
    )
    assert mvp_res.status_code == 200

    # 7. End Round 1
    end_r1 = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r1_id}/end-round")
    assert end_r1.status_code == 200
    assert end_r1.json()["is_final_round"] is False
    assert end_r1.json()["tournament_completed"] is False

    # 8. Create Round 2 (Final Round) — should inherit qualifiers from Round 1
    r2_payload = {
        "stage_name": "Grand Finals",
        "competition_format": "Battle Royale",
        "is_final_round": True,
    }
    r2_res = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds", json=r2_payload)
    assert r2_res.status_code == 200
    r2_data = r2_res.json()["round"]
    assert r2_data["round_number"] == 2
    assert r2_data["is_final_round"] is True
    assert len(r2_data["eligible_contenders"]) == 2
    assert {c["id"] for c in r2_data["eligible_contenders"]} == {"team_1", "team_2"}

    r2_id = r2_data["id"]

    # 9. Set Champion on Final Round
    champ_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r2_id}/qualifiers",
        json={"champion_id": "team_1", "champion_name": "Team Alpha 1"}
    )
    assert champ_res.status_code == 200
    assert champ_res.json()["champion_id"] == "team_1"

    # 10. Set Final MVP and Tournament MVP
    await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r2_id}/mvp",
        json={"player_id": "cap_1", "player_name": "SniperKing"}
    )
    await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/tournament-mvp",
        json={"player_id": "cap_1", "player_name": "SniperKing", "stats": "Tournament MVP 15 Kills"}
    )

    # 11. Configure Final Results with conflict check
    # Conflict test: recipient of 1st place is team_2, but champion was team_1
    conflict_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/final-results",
        json={"results": [
            {"award_name": "1st Place Champions", "recipient_type": "TEAM", "recipient_id": "team_2", "recipient_name": "Team Alpha 2", "display_order": 1}
        ]}
    )
    assert conflict_res.status_code == 200
    assert conflict_res.json()["champion_conflict"] is True

    # Correct final results: recipient is champion team_1
    correct_res = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/final-results",
        json={"results": [
            {"award_name": "1st Place Champions", "recipient_type": "TEAM", "recipient_id": "team_1", "recipient_name": "Team Alpha 1", "display_order": 1, "prize_amount": 30000},
            {"award_name": "Runner Up", "recipient_type": "TEAM", "recipient_id": "team_2", "recipient_name": "Team Alpha 2", "display_order": 2, "prize_amount": 15000},
        ]}
    )
    assert correct_res.status_code == 200
    assert correct_res.json()["champion_conflict"] is False

    # 12. End Final Round -> Tournament is finalized as COMPLETED
    end_r2 = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r2_id}/end-round")
    assert end_r2.status_code == 200
    assert end_r2.json()["tournament_completed"] is True

    # Verify tournament is completed in DB
    updated_t = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert updated_t["status"] == "COMPLETED"
    assert updated_t["champion"]["id"] == "team_1"
    assert updated_t["tournament_mvp"]["player_name"] == "SniperKing"
    assert len(updated_t["final_results"]) == 2

    # Verify no new rounds can be created once completed
    blocked_round = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds",
        json={"stage_name": "Bonus Round", "competition_format": "Battle Royale"}
    )
    assert blocked_round.status_code == 400


@pytest.mark.asyncio
async def test_knockout_round_and_matches_workflow(mock_db, make_client):
    admin = User(id="admin_1", username="admin_1", email="admin1@example.com", role="SUPER_ADMIN")
    client = make_client(admin)

    tourn_id = "t_ko_test"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": tourn_id,
        "title": "Valorant Knockout Cup",
        "game_id": "valorant",
        "participation_mode": "SQUAD",
        "status": "REGISTRATION_CLOSED",
        "enable_mvp": False,
        "format": "Knockout",
    })

    # Seed 2 teams
    for t_id in ["team_a", "team_b"]:
        await mock_db.teams.insert_one({"_id": t_id, "id": t_id, "name": f"Team {t_id.upper()}", "tag": "KO"})
        await mock_db.tournament_registrations.insert_one({
            "_id": f"reg_{t_id}",
            "tournament_id": tourn_id,
            "team_id": t_id,
            "payment_status": "PAID",
        })

    # 1. Create Final Knockout Round
    r_res = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds", json={
        "stage_name": "Grand Finals",
        "competition_format": "Knockout",
        "is_final_round": True,
    })
    assert r_res.status_code == 200
    r_id = r_res.json()["round"]["id"]

    # 2. Create Knockout Match with strictly 3 fields
    m_res = await client.post(f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r_id}/matches", json={
        "match_number": 1,
        "team_a_id": "team_a",
        "team_b_id": "team_b",
        "scheduled_time": "20:00 UTC",
    })
    assert m_res.status_code == 200
    m_data = m_res.json()["match"]
    assert m_data["match_number"] == 1
    assert m_data["team_a_id"] == "team_a"
    assert m_data["team_b_id"] == "team_b"

    match_id = m_data["id"]

    # 3. Save match result (Team A wins 13-9)
    res_save = await client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/rounds/{r_id}/matches/{match_id}/result",
        json={"winner_id": "team_a", "score_a": 13, "score_b": 9}
    )
    assert res_save.status_code == 200
    assert res_save.json()["round_completed"] is True
    assert res_save.json()["champion_id"] == "team_a"

    # Verify tournament champion updated
    t_doc = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert t_doc["champion"]["id"] == "team_a"
