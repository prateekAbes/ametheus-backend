import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from pymongo.errors import DuplicateKeyError

from app.core.database import get_db, set_database
from app.main import app
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.user import User
from app.models.base import generate_uuid
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin
from app.services.live_match_service import (
    sync_live_match_for_tournament,
    reconcile_all_live_matches,
    ensure_live_match_indexes,
)
from app.services.bracket_engine import (
    build_single_elimination_bracket,
    advance_bracket_match,
)

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_live_match_test"]
    set_database(db)
    await ensure_live_match_indexes(db)
    return db

@pytest.fixture
def make_client(mock_db):
    from fastapi import Header, Depends, HTTPException
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
            user_map[user.id] = user
            headers["Authorization"] = f"Bearer {user.id}"
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test", headers=headers)

    return _create


# Scenario 1: Tournament with zero matches
@pytest.mark.asyncio
async def test_scenario_1_tournament_zero_matches(mock_db, make_client):
    tourn_id = generate_uuid()
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Zero Match Championship",
        "slug": "zero-match-champ",
        "status": "REGISTRATION_OPEN",
        "format": "Knockout",
        "series_format": "BO1",
        "game_slug": "valorant",
        "game_name": "Valorant",
    })

    # Synchronize live match - must return None when no persisted matches exist
    synced = await sync_live_match_for_tournament(tourn_id, mock_db)
    assert synced is None

    # Repeated calls must remain clean and idempotent (no phantom records)
    await sync_live_match_for_tournament(tourn_id, mock_db)
    await sync_live_match_for_tournament(tourn_id, mock_db)

    count = await mock_db.live_matches.count_documents({"tournament_id": tourn_id})
    assert count == 0

    client = make_client()
    resp = await client.get("/api/v1/matches")
    assert resp.status_code == 200
    cards = resp.json()
    # Critical requirement: Database is source of truth. Zero matches in db.matches must return empty list (clean empty state)
    assert len(cards) == 0


# Scenario 2: Tournament with 3 knockout matches (Shows Match 1, exactly 1 Live Card)
@pytest.mark.asyncio
async def test_scenario_2_knockout_three_matches_shows_match_1(mock_db, make_client):
    tourn_id = generate_uuid()
    org_user = User(id="org_1", username="organizer_one", email="org1@test.com", role="ORGANIZER")
    await mock_db.games.insert_one({"_id": "game_val", "id": "game_val", "slug": "valorant", "name": "Valorant"})
    tourn = Tournament(
        id=tourn_id,
        title="Apex Knockout Masters",
        slug="apex-knockout-masters",
        game_id="game_val",
        starts_at=datetime.now(timezone.utc).isoformat(),
        roster_lock_time=datetime.now(timezone.utc).isoformat(),
        format="Knockout",
        series_format="BO1",
        status="LIVE",
        organizer_id=org_user.id,
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    # Create 4 competing teams & registrations
    team_ids = []
    for i in range(1, 5):
        t_id = f"team_k_{i}"
        team_ids.append(t_id)
        await mock_db.teams.insert_one({
            "_id": t_id,
            "id": t_id,
            "name": f"Team Alpha {i}",
            "tag": f"A{i}",
            "captain_id": f"cap_{i}",
        })
        await mock_db.tournament_registrations.insert_one({
            "id": generate_uuid(),
            "tournament_id": tourn_id,
            "team_id": t_id,
            "payment_status": "PAID",
        })

    # Build bracket (Semifinals 1, Semifinals 2, Grand Final)
    bracket_res = await build_single_elimination_bracket(tourn, mock_db)
    matches_in_db = await mock_db.matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(matches_in_db) == 3

    # Exactly 1 Live Match card exists
    live_docs = await mock_db.live_matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(live_docs) == 1
    live_card = live_docs[0]
    assert live_card["current_encounter_number"] == 1
    assert live_card["team_a_id"] == team_ids[0]
    assert live_card["team_b_id"] == team_ids[1]

    client = make_client()
    resp = await client.get("/api/v1/matches")
    assert resp.status_code == 200
    cards = [c for c in resp.json() if c["tournament_id"] == tourn_id]
    assert len(cards) == 1
    assert cards[0]["order"] == 1
    assert cards[0]["team_a"]["id"] == team_ids[0]


# Scenario 3: Match 1 finishes -> same card updates to Match 2
@pytest.mark.asyncio
async def test_scenario_3_match_1_finishes_updates_card_to_match_2(mock_db, make_client):
    tourn_id = generate_uuid()
    org_user = User(id="org_1", username="organizer_one", email="org1@test.com", role="ORGANIZER")
    await mock_db.games.insert_one({"_id": "game_val", "id": "game_val", "slug": "valorant", "name": "Valorant"})
    tourn = Tournament(
        id=tourn_id,
        title="Apex Knockout Masters",
        slug="apex-knockout-masters",
        game_id="game_val",
        starts_at=datetime.now(timezone.utc).isoformat(),
        roster_lock_time=datetime.now(timezone.utc).isoformat(),
        format="Knockout",
        series_format="BO1",
        status="LIVE",
        organizer_id=org_user.id,
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    for i in range(1, 5):
        t_id = f"team_k_{i}"
        await mock_db.teams.insert_one({"_id": t_id, "id": t_id, "name": f"Team Alpha {i}", "tag": f"A{i}"})
        await mock_db.tournament_registrations.insert_one({"tournament_id": tourn_id, "team_id": t_id, "payment_status": "PAID"})

    await build_single_elimination_bracket(tourn, mock_db)

    # Fetch Match 1
    m1_doc = await mock_db.matches.find_one({"tournament_id": tourn_id, "match_number": 1, "round_number": 1})
    m1 = Match.from_mongo(m1_doc)

    # Complete Match 1: Team 1 wins
    await mock_db.matches.update_one({"_id": m1_doc["_id"]}, {"$set": {"score_a": 13, "score_b": 9}})
    await advance_bracket_match(tourn_id, m1, m1.team_a_id, mock_db)

    # Live Match card updates to Match 2
    live_docs = await mock_db.live_matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(live_docs) == 1
    updated_card = live_docs[0]
    assert updated_card["current_encounter_number"] == 2
    assert updated_card["team_a_id"] == "team_k_3"
    assert updated_card["team_b_id"] == "team_k_4"

    # API returns exactly 1 card
    client = make_client()
    resp = await client.get("/api/v1/matches")
    cards = [c for c in resp.json() if c["tournament_id"] == tourn_id]
    assert len(cards) == 1
    assert cards[0]["order"] == 2
    assert cards[0]["team_a"]["id"] == "team_k_3"


# Scenario 4: Match 2 finishes -> same card updates to Grand Final (Match 3)
@pytest.mark.asyncio
async def test_scenario_4_match_2_finishes_updates_card_to_grand_final(mock_db, make_client):
    tourn_id = generate_uuid()
    org_user = User(id="org_1", username="organizer_one", email="org1@test.com", role="ORGANIZER")
    await mock_db.games.insert_one({"_id": "game_val", "id": "game_val", "slug": "valorant", "name": "Valorant"})
    tourn = Tournament(
        id=tourn_id,
        title="Apex Knockout Masters",
        slug="apex-knockout-masters",
        game_id="game_val",
        starts_at=datetime.now(timezone.utc).isoformat(),
        roster_lock_time=datetime.now(timezone.utc).isoformat(),
        format="Knockout",
        series_format="BO1",
        status="LIVE",
        organizer_id=org_user.id,
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    for i in range(1, 5):
        t_id = f"team_k_{i}"
        await mock_db.teams.insert_one({"_id": t_id, "id": t_id, "name": f"Team Alpha {i}", "tag": f"A{i}"})
        await mock_db.tournament_registrations.insert_one({"tournament_id": tourn_id, "team_id": t_id, "payment_status": "PAID"})

    await build_single_elimination_bracket(tourn, mock_db)

    # Complete Match 1
    m1_doc = await mock_db.matches.find_one({"tournament_id": tourn_id, "match_number": 1, "round_number": 1})
    m1 = Match.from_mongo(m1_doc)
    await advance_bracket_match(tourn_id, m1, m1.team_a_id, mock_db)

    # Complete Match 2: Team 4 wins
    m2_doc = await mock_db.matches.find_one({"tournament_id": tourn_id, "match_number": 2, "round_number": 1})
    m2 = Match.from_mongo(m2_doc)
    await advance_bracket_match(tourn_id, m2, m2.team_b_id, mock_db)

    # Live Match card updates to Grand Final (Round 2 Match 1)
    live_docs = await mock_db.live_matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(live_docs) == 1
    gf_card = live_docs[0]
    assert "Grand Final" in gf_card["current_round_name"] or "Round 2" in gf_card["current_round_name"]
    assert gf_card["team_a_id"] == "team_k_1"
    assert gf_card["team_b_id"] == "team_k_4"

    client = make_client()
    resp = await client.get("/api/v1/matches")
    cards = [c for c in resp.json() if c["tournament_id"] == tourn_id]
    assert len(cards) == 1
    assert cards[0]["team_a"]["id"] == "team_k_1"
    assert cards[0]["team_b"]["id"] == "team_k_4"


# Scenario 5: All matches finish -> card displays completion / winner, total cards remains 1
@pytest.mark.asyncio
async def test_scenario_5_all_matches_finish_card_displays_winner(mock_db, make_client):
    tourn_id = generate_uuid()
    org_user = User(id="org_1", username="organizer_one", email="org1@test.com", role="ORGANIZER")
    await mock_db.games.insert_one({"_id": "game_val", "id": "game_val", "slug": "valorant", "name": "Valorant"})
    tourn = Tournament(
        id=tourn_id,
        title="Apex Knockout Masters",
        slug="apex-knockout-masters",
        game_id="game_val",
        starts_at=datetime.now(timezone.utc).isoformat(),
        roster_lock_time=datetime.now(timezone.utc).isoformat(),
        format="Knockout",
        series_format="BO1",
        status="LIVE",
        organizer_id=org_user.id,
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    for i in range(1, 5):
        t_id = f"team_k_{i}"
        await mock_db.teams.insert_one({"_id": t_id, "id": t_id, "name": f"Team Alpha {i}", "tag": f"A{i}"})
        await mock_db.tournament_registrations.insert_one({"tournament_id": tourn_id, "team_id": t_id, "payment_status": "PAID"})

    await build_single_elimination_bracket(tourn, mock_db)

    m1_doc = await mock_db.matches.find_one({"tournament_id": tourn_id, "match_number": 1, "round_number": 1})
    m1 = Match.from_mongo(m1_doc)
    await advance_bracket_match(tourn_id, m1, m1.team_a_id, mock_db)

    m2_doc = await mock_db.matches.find_one({"tournament_id": tourn_id, "match_number": 2, "round_number": 1})
    m2 = Match.from_mongo(m2_doc)
    await advance_bracket_match(tourn_id, m2, m2.team_b_id, mock_db)

    # Complete Grand Final: Team 1 crowned Champion!
    gf_doc = await mock_db.matches.find_one({"tournament_id": tourn_id, "round_number": 2})
    gf = Match.from_mongo(gf_doc)
    await advance_bracket_match(tourn_id, gf, "team_k_1", mock_db)

    # Live Match card remains 1, displays COMPLETED & Champion
    live_docs = await mock_db.live_matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(live_docs) == 1
    final_card = live_docs[0]
    assert final_card["status"] == "COMPLETED"
    assert final_card["winner_team_id"] == "team_k_1"

    client = make_client()
    resp = await client.get("/api/v1/matches?status=COMPLETED")
    assert resp.status_code == 200
    cards = [c for c in resp.json() if c["tournament_id"] == tourn_id]
    assert len(cards) == 1
    assert cards[0]["status"] == "COMPLETED"
    assert cards[0]["winner_team_id"] == "team_k_1"


# Scenario 6: Battle Royale Round 1 (25 teams) created -> exactly 1 Live Card showing 25 squads
@pytest.mark.asyncio
async def test_scenario_6_battle_royale_round_1_twenty_five_teams(mock_db, make_client):
    tourn_id = generate_uuid()
    org_user = User(id="org_br", username="br_organizer", email="br@test.com", role="ORGANIZER")
    tourn_doc = {
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Ametheus BR Championship",
        "slug": "ametheus-br-champ",
        "format": "Battle Royale",
        "series_format": "BO1",
        "status": "LIVE",
        "organizer_id": org_user.id,
        "game_slug": "bgmi",
        "game_name": "BGMI",
    }
    await mock_db.tournaments.insert_one(tourn_doc)

    teams = []
    for i in range(1, 26):
        tid = f"br_squad_{i}"
        teams.append({"id": tid, "name": f"Squad {i}"})
        await mock_db.teams.insert_one({"_id": tid, "id": tid, "name": f"Squad {i}"})
        await mock_db.tournament_registrations.insert_one({
            "id": generate_uuid(),
            "tournament_id": tourn_id,
            "team_id": tid,
            "payment_status": "PAID",
        })

    # Generate Battle Royale Match
    match_id = generate_uuid()
    m_doc = {
        "_id": match_id,
        "id": match_id,
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "round_name": "Round 1",
        "stage": "Round 1 Lobby",
        "format": "Battle Royale",
        "match_type": "BATTLE_ROYALE",
        "status": "SCHEDULED",
        "teams": teams,
        "team_ids": [t["id"] for t in teams],
        "squads_count": 25,
    }
    await mock_db.matches.insert_one(m_doc)

    await sync_live_match_for_tournament(tourn_id, mock_db)

    # Exactly 1 Live Card exists
    live_docs = await mock_db.live_matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(live_docs) == 1
    br_card = live_docs[0]
    assert br_card["is_battle_royale"] is True
    assert br_card["current_round_name"] == "Round 1"
    assert len(br_card["teams"]) == 25

    client = make_client()
    resp = await client.get("/api/v1/matches")
    cards = [c for c in resp.json() if c["tournament_id"] == tourn_id]
    assert len(cards) == 1
    assert cards[0]["match_type"] == "BATTLE_ROYALE"
    assert len(cards[0]["teams"]) == 25


# Scenario 7: Battle Royale Round 1 finishes (10 advance to Round 2) -> same card updates to Round 2
@pytest.mark.asyncio
async def test_scenario_7_battle_royale_round_progression(mock_db, make_client):
    tourn_id = generate_uuid()
    org_user = User(id="org_br", username="br_organizer", email="br@test.com", role="ORGANIZER")
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Ametheus BR Championship",
        "slug": "ametheus-br-champ",
        "format": "Battle Royale",
        "status": "LIVE",
        "organizer_id": org_user.id,
    })

    teams = [{"id": f"br_squad_{i}", "name": f"Squad {i}"} for i in range(1, 26)]
    m1_id = generate_uuid()
    await mock_db.matches.insert_one({
        "_id": m1_id,
        "id": m1_id,
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "round_name": "Round 1",
        "format": "Battle Royale",
        "status": "ENDED",
        "teams": teams,
        "team_ids": [t["id"] for t in teams],
    })

    await sync_live_match_for_tournament(tourn_id, mock_db)

    client = make_client(org_user)
    # Submit Round 1 results: Top 10 squads qualify
    qualifier_ids = [f"br_squad_{i}" for i in range(1, 11)]
    placements = [{"team_id": q, "placement": idx + 1, "kills": 5, "qualified": True} for idx, q in enumerate(qualifier_ids)]
    payload = {
        "qualifier_team_ids": qualifier_ids,
        "qualifiers_count": 10,
        "is_final_round": False,
        "advance_to_next_round": True,
        "next_round_name": "Round 2",
        "placements": placements,
    }

    submit_resp = await client.post(f"/api/v1/matches/{m1_id}/battle-royale-results", json=payload)
    assert submit_resp.status_code == 200

    # Verify: Exactly 1 Live Match card exists in db.live_matches
    live_docs = await mock_db.live_matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(live_docs) == 1
    updated_br_card = live_docs[0]
    assert updated_br_card["current_round_name"] == "Round 2"
    assert len(updated_br_card["teams"]) == 10

    # Historical Round 1 match is intact in db.matches
    r1_match = await mock_db.matches.find_one({"_id": m1_id})
    assert r1_match["status"] == "COMPLETED"
    assert len(r1_match["teams"]) == 25

    # Public matches API returns exactly 1 card
    resp = await client.get("/api/v1/matches")
    cards = [c for c in resp.json() if c["tournament_id"] == tourn_id]
    assert len(cards) == 1
    assert cards[0]["current_round_name"] == "Round 2"
    assert len(cards[0]["teams"]) == 10


# Scenario 8: Multiple tournaments running simultaneously -> each has exactly 1 Live Card
@pytest.mark.asyncio
async def test_scenario_8_multiple_tournaments_simultaneously(mock_db, make_client):
    t_ids = [f"sim_tourn_{i}" for i in range(1, 4)]
    for idx, tid in enumerate(t_ids, start=1):
        await mock_db.tournaments.insert_one({
            "_id": tid,
            "id": tid,
            "title": f"Championship League {idx}",
            "slug": f"champ-league-{idx}",
            "status": "LIVE",
            "format": "Knockout" if idx % 2 != 0 else "Battle Royale",
        })
        m_id = f"m_sim_{idx}"
        await mock_db.matches.insert_one({
            "_id": m_id,
            "id": m_id,
            "tournament_id": tid,
            "round_number": 1,
            "match_number": 1,
            "status": "LIVE",
            "format": "Knockout" if idx % 2 != 0 else "Battle Royale",
            "team_a_name": f"Team A {idx}",
            "team_b_name": f"Team B {idx}",
        })
        await sync_live_match_for_tournament(tid, mock_db)

    # Exactly 3 tournaments = exactly 3 live cards
    total_live = await mock_db.live_matches.count_documents({})
    assert total_live == 3

    client = make_client()
    resp = await client.get("/api/v1/matches")
    cards = resp.json()
    assert len(cards) == 3
    retrieved_tids = set(c["tournament_id"] for c in cards)
    assert retrieved_tids == set(t_ids)


# Scenario 9: Concurrent update requests -> idempotent with 0 duplicates
@pytest.mark.asyncio
async def test_scenario_9_concurrent_update_requests_idempotency(mock_db):
    tourn_id = generate_uuid()
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Concurrency Masters",
        "status": "LIVE",
        "format": "Knockout",
    })
    await mock_db.matches.insert_one({
        "_id": generate_uuid(),
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "status": "LIVE",
        "roster_status": "CONFIRMED",
    })

    # Spawn 10 concurrent synchronization tasks
    tasks = [sync_live_match_for_tournament(tourn_id, mock_db) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All succeeded
    assert all(r is not None for r in results)

    # Exactly 1 record in database
    count = await mock_db.live_matches.count_documents({"tournament_id": tourn_id})
    assert count == 1


# Scenario 10: Database constraint test -> duplicate insert rejected by unique index
@pytest.mark.asyncio
async def test_scenario_10_database_unique_index_constraint(mock_db):
    tourn_id = "test_unique_constraint_tid"

    # First insert succeeds
    await mock_db.live_matches.insert_one({
        "tournament_id": tourn_id,
        "title": "First Entry",
        "created_at": datetime.now(timezone.utc),
    })

    # Second insert with identical tournament_id must raise DuplicateKeyError
    with pytest.raises(DuplicateKeyError):
        await mock_db.live_matches.insert_one({
            "tournament_id": tourn_id,
            "title": "Duplicate Entry Attempt",
            "created_at": datetime.now(timezone.utc),
        })

    # Verify only 1 document exists
    count = await mock_db.live_matches.count_documents({"tournament_id": tourn_id})
    assert count == 1


# Scenario 11: Querying match history for a tournament -> encounters remain intact
@pytest.mark.asyncio
async def test_scenario_11_tournament_match_history_preservation(mock_db, make_client):
    tourn_id = generate_uuid()
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "History Preservation Tourney",
        "status": "LIVE",
        "format": "Knockout",
    })

    # 3 separate matches in db.matches
    for i in range(1, 4):
        mid = f"hist_m_{i}"
        await mock_db.matches.insert_one({
            "_id": mid,
            "id": mid,
            "tournament_id": tourn_id,
            "round_number": 1 if i <= 2 else 2,
            "match_number": i,
            "stage": f"Match {i}",
            "status": "COMPLETED" if i <= 2 else "LIVE",
            "score_a": 13,
            "score_b": 7,
            "winner_team_id": f"winner_{i}",
        })

    await sync_live_match_for_tournament(tourn_id, mock_db)

    # Public list without tournament_id returns strictly 1 Live Match card
    client = make_client()
    public_resp = await client.get("/api/v1/matches")
    assert public_resp.status_code == 200
    assert len(public_resp.json()) == 1

    # Detailed list with tournament_id returns all 3 encounters
    tourn_resp = await client.get(f"/api/v1/matches?tournament_id={tourn_id}")
    assert tourn_resp.status_code == 200
    encounters = tourn_resp.json()
    assert len(encounters) == 3
    assert set(e["id"] for e in encounters) == {"hist_m_1", "hist_m_2", "hist_m_3"}

    # Encounter history on live document contains completed encounters
    live_doc = await mock_db.live_matches.find_one({"tournament_id": tourn_id})
    assert live_doc is not None
    assert len(live_doc["encounter_history"]) == 2
