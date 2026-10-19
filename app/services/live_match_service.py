import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pymongo import ReturnDocument
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.base import get_utc_now, generate_uuid
from app.models.live_match import LiveMatch
from app.core.ws import ws_manager

logger = logging.getLogger(__name__)

async def ensure_live_match_indexes(db: AsyncIOMotorDatabase):
    """
    Ensures that exactly one Live Match document exists per tournament at the database level.
    Deduplicates any legacy records and creates a strict unique index on tournament_id.
    """
    try:
        # Check and resolve any existing duplicate records before index creation
        pipeline = [
            {"$group": {"_id": "$tournament_id", "count": {"$sum": 1}, "docs": {"$push": "$_id"}}},
            {"$match": {"count": {"$gt": 1}}}
        ]
        duplicates = await db.live_matches.aggregate(pipeline).to_list(length=100)
        for dup in duplicates:
            # Keep the newest record, remove older duplicate records
            keep_id = dup["docs"][-1]
            remove_ids = dup["docs"][:-1]
            await db.live_matches.delete_many({"_id": {"$in": remove_ids}})

        # Create unique constraint
        await db.live_matches.create_index("tournament_id", unique=True)
        await db.live_matches.create_index("status")
        await db.live_matches.create_index("game_id")
        await db.live_matches.create_index("game_slug")
    except Exception as e:
        logger.warning(f"Live match index setup notice: {e}")

def determine_current_encounter(encounters: List[Dict[str, Any]], tournament: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], str]:
    """
    Determines the current active encounter for a tournament.
    Priority order:
    1. Active encounter: Status LIVE or IN_PROGRESS
    2. Check-in open encounter: Status CHECK_IN_OPEN
    3. Next scheduled encounter: Status SCHEDULED (earliest round and match_number)
    4. If all encounters are completed: The final completed encounter
    """
    if not encounters:
        t_status = tournament.get("status", "SCHEDULED").upper()
        if t_status in ["LIVE", "ONGOING", "IN_PROGRESS"]:
            return None, "LIVE"
        elif t_status == "COMPLETED":
            return None, "COMPLETED"
        return None, "SCHEDULED"

    # Sort encounters by round_number, then match_number, then order
    sorted_encounters = sorted(
        encounters,
        key=lambda e: (
            int(e.get("round_number") or 1),
            int(e.get("match_number") or 1),
            int(e.get("order") or 0)
        )
    )

    # 1. Live or In-Progress encounter
    for enc in sorted_encounters:
        st = str(enc.get("status", "")).upper()
        if st in ["LIVE", "IN_PROGRESS"]:
            return enc, "LIVE"

    # 2. Check-in open encounter
    for enc in sorted_encounters:
        st = str(enc.get("status", "")).upper()
        if st == "CHECK_IN_OPEN":
            return enc, "CHECK_IN_OPEN"

    # 3. Next scheduled encounter
    for enc in sorted_encounters:
        st = str(enc.get("status", "")).upper()
        if st in ["SCHEDULED", "PENDING_VERIFICATION", "DRAFT"]:
            t_status = tournament.get("status", "SCHEDULED").upper()
            return enc, "LIVE" if t_status in ["LIVE", "ONGOING"] else "SCHEDULED"

    # 4. If all completed, return the final encounter
    final_enc = sorted_encounters[-1]
    return final_enc, "COMPLETED"

async def sync_live_match_for_tournament(
    tournament_id: str,
    db: AsyncIOMotorDatabase
) -> Optional[Dict[str, Any]]:
    """
    Canonical idempotent synchronization function for the single Live Match per tournament.
    - Idempotently creates or updates exactly one live_matches record for the tournament.
    - Preserves all historical individual encounter results in db.matches.
    - Updates the Live Match card's current encounter, participants, and status.
    - Broadcasts real-time WebSocket ticker updates.
    """
    if not tournament_id:
        return None

    # 1. Resolve Tournament
    tourn = await db.tournaments.find_one({
        "$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]
    })
    if not tourn:
        return None

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug")

    # 2. Resolve Game
    game_doc = None
    gid = tourn.get("game_id")
    if gid:
        game_doc = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

    game_slug = game_doc.get("slug", "valorant") if game_doc else "valorant"
    game_name = game_doc.get("name", "Valorant") if game_doc else "Valorant"

    # Tournament cancellation / deletion check
    tourn_status = str(tourn.get("status", "")).upper()
    if tourn_status in ["CANCELLED", "DELETED"] or tourn.get("is_deleted"):
        await db.live_matches.delete_one({"tournament_id": tid})
        return None

    # 3. Query all authentic, persisted encounters for this tournament
    enc_cursor = db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "status": {"$nin": ["CANCELLED", "DELETED"]},
        "is_deleted": {"$ne": True},
        "is_dummy": {"$ne": True},
    })
    encounters = await enc_cursor.to_list(length=300)

    # Filter out draft or invalid matches
    valid_encounters = [
        e for e in encounters
        if e.get("roster_status") != "DRAFT"
    ]

    # Check for tournament rounds in db.tournament_rounds (e.g. Battle Royale or round-managed tournaments)
    rounds_cursor = db.tournament_rounds.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "status": {"$nin": ["CANCELLED", "DELETED"]},
    }).sort("round_number", 1)
    rounds = await rounds_cursor.to_list(length=100)

    # Always merge tournament_rounds into valid_encounters.
    # Previously this block was gated on `not valid_encounters`, which meant any stale
    # db.matches record would prevent Battle Royale rounds from appearing at all.
    # Now we synthesize an encounter for each tournament_round whose ID is not already
    # represented in valid_encounters from db.matches.
    if rounds:
        existing_ids = {
            str(e.get("_id", e.get("id", "")))
            for e in valid_encounters
        }
        for r in rounds:
            r_id = str(r.get("_id", r.get("id", "")))
            if r_id in existing_ids:
                # Already covered by a real db.matches record — skip synthesis
                continue
            r_st = str(r.get("status", "UPCOMING")).upper()
            enc_status = "LIVE" if r_st in ["LIVE", "IN_PROGRESS"] else ("COMPLETED" if r_st == "COMPLETED" else "SCHEDULED")
            synth_enc = {
                "_id": r_id,
                "id": r_id,
                "tournament_id": tid,
                "round_number": r.get("round_number", 1),
                "round_name": r.get("round_name") or r.get("stage_name") or f"Round {r.get('round_number', 1)}",
                "stage": r.get("stage_name") or r.get("round_name") or f"Round {r.get('round_number', 1)}",
                "format": r.get("format", "Battle Royale"),
                "status": enc_status,
                "room_id": r.get("room_id"),
                "room_password": r.get("room_password"),
                "lobby_code": r.get("room_id"),
                "lobby_password": r.get("room_password"),
                "credentials_released": bool(r.get("credentials_released", False)),
                "teams": r.get("eligible_contenders") or [],
                "team_ids": [c.get("id") for c in (r.get("eligible_contenders") or []) if c.get("id")],
                "squads_count": len(r.get("eligible_contenders") or []),
                "qualifier_team_ids": r.get("qualifier_ids") or [],
                "eliminated_ids": r.get("eliminated_ids") or [],
                "placements": r.get("placements") or [],
                "winner_team_id": r.get("winner_id") or r.get("winner_team_id"),
                "order": r.get("round_number", 1),
                "scheduled_at": r.get("scheduled_at") or tourn.get("starts_at"),
            }
            valid_encounters.append(synth_enc)

    # If the tournament has neither matches nor rounds, it has NO live or upcoming match
    if not valid_encounters:
        await db.live_matches.delete_one({"tournament_id": tid})
        return None

    # Check if any tournament round is currently LIVE
    active_live_round = next((r for r in rounds if str(r.get("status", "")).upper() in ["LIVE", "IN_PROGRESS"]), None)

    # 4. Determine current encounter
    current_encounter, calculated_status = determine_current_encounter(valid_encounters, tourn)
    if not current_encounter:
        await db.live_matches.delete_one({"tournament_id": tid})
        return None

    # Round or Tournament status override
    if tourn_status == "COMPLETED":
        calculated_status = "COMPLETED"
    elif (tourn_status in ["LIVE", "ONGOING"] or active_live_round is not None) and calculated_status != "COMPLETED":
        calculated_status = "LIVE"

    fmt = tourn.get("format") or tourn.get("tournament_type") or "Knockout"
    is_br = (
        "battle royale" in fmt.lower() or
        (current_encounter and "battle royale" in str(current_encounter.get("format", "")).lower()) or
        (current_encounter and "battle_royale" in str(current_encounter.get("match_type", "")).lower())
    )

    # 5. Extract current encounter data from the real persisted match
    current_enc_id = str(current_encounter.get("_id", current_encounter.get("id")))
    current_enc_no = int(current_encounter.get("match_number") or current_encounter.get("order") or 1)
    current_round_no = int(current_encounter.get("round_number") or 1)
    current_round_name = (
        current_encounter.get("round_name") or
        current_encounter.get("stage") or
        f"Round {current_round_no}"
    )
    current_enc_title = f"Match {current_enc_no}"
    team_a_id = None
    team_b_id = None
    team_a_name = None
    team_b_name = None
    team_a_doc = None
    team_b_doc = None
    score_a = None
    score_b = None
    teams_list = []
    team_ids_list = []
    squads_count = 0
    placements = []
    qualifier_team_ids = []
    winner_team_id = tourn.get("winner_team_id")
    room_id = None
    room_password = None
    lobby_code = None
    lobby_password = None
    credentials_released = False
    youtube_live_url = tourn.get("live_stream_url")
    custom_instructions = None
    server_region = tourn.get("server_region", "AWS Mumbai (ap-south-1)")
    scheduled_at = tourn.get("starts_at") or tourn.get("scheduled_at")
    scheduled_date = tourn.get("scheduled_date")
    scheduled_time = tourn.get("scheduled_time", "TBD")
    check_in_status = {}

    if current_encounter:
        current_enc_id = str(current_encounter.get("_id", current_encounter.get("id")))
        current_enc_no = int(current_encounter.get("match_number") or current_encounter.get("order") or 1)
        current_round_no = int(current_encounter.get("round_number") or 1)
        current_round_name = (
            current_encounter.get("round_name") or
            current_encounter.get("stage") or
            f"Round {current_round_no}"
        )
        current_enc_title = f"Match {current_enc_no}"

        team_a_id = current_encounter.get("team_a_id")
        team_b_id = current_encounter.get("team_b_id")
        team_a_name = current_encounter.get("team_a_name")
        team_b_name = current_encounter.get("team_b_name")

        score_a = current_encounter.get("score_a")
        score_b = current_encounter.get("score_b")
        if current_encounter.get("winner_team_id"):
            winner_team_id = current_encounter.get("winner_team_id")

        teams_list = list(current_encounter.get("teams") or [])
        team_ids_list = list(current_encounter.get("team_ids") or [])
        squads_count = len(teams_list) or len(team_ids_list) or int(current_encounter.get("squads_count") or 0)
        placements = list(current_encounter.get("placements") or [])
        qualifier_team_ids = list(current_encounter.get("qualifier_team_ids") or [])

        room_id = current_encounter.get("room_id") or current_encounter.get("lobby_code")
        room_password = current_encounter.get("room_password") or current_encounter.get("lobby_password")
        lobby_code = current_encounter.get("lobby_code") or current_encounter.get("room_id")
        lobby_password = current_encounter.get("lobby_password") or current_encounter.get("room_password")
        credentials_released = bool(current_encounter.get("credentials_released", False))

        if current_encounter.get("youtube_live_url"):
            youtube_live_url = current_encounter.get("youtube_live_url")
        elif current_encounter.get("live_link"):
            youtube_live_url = current_encounter.get("live_link")

        custom_instructions = current_encounter.get("custom_instructions") or current_encounter.get("match_message")
        server_region = current_encounter.get("server_region") or server_region
        scheduled_at = current_encounter.get("scheduled_at") or scheduled_at
        scheduled_date = current_encounter.get("scheduled_date") or scheduled_date
        scheduled_time = current_encounter.get("scheduled_time") or scheduled_time
        check_in_status = dict(current_encounter.get("check_in_status") or {})

        # Resolve team details for Team A and Team B
        if team_a_id:
            team_a_obj = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]})
            if team_a_obj:
                team_a_name = team_a_obj.get("name") or team_a_name
                team_a_doc = {"id": team_a_id, "name": team_a_name, "tag": team_a_obj.get("tag", "TEAM"), "score": score_a}
        if not team_a_doc and team_a_name:
            team_a_doc = {"id": team_a_id, "name": team_a_name, "score": score_a}

        if team_b_id:
            team_b_obj = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]})
            if team_b_obj:
                team_b_name = team_b_obj.get("name") or team_b_name
                team_b_doc = {"id": team_b_id, "name": team_b_name, "tag": team_b_obj.get("tag", "TEAM"), "score": score_b}
        if not team_b_doc and team_b_name:
            team_b_doc = {"id": team_b_id, "name": team_b_name, "score": score_b}

    # 6. Assemble Historical Encounter Summary
    completed_encounters = [
        {
            "encounter_id": str(e.get("_id", e.get("id"))),
            "match_number": e.get("match_number"),
            "round_number": e.get("round_number"),
            "round_name": e.get("round_name") or e.get("stage"),
            "team_a_name": e.get("team_a_name"),
            "team_b_name": e.get("team_b_name"),
            "score_a": e.get("score_a"),
            "score_b": e.get("score_b"),
            "winner_team_id": e.get("winner_team_id"),
            "status": e.get("status"),
            "is_battle_royale": is_br,
        }
        for e in encounters
        if str(e.get("status", "")).upper() == "COMPLETED"
    ]

    # 7. Formulate Canonical Persistent Live Match Document
    live_match_data = {
        "tournament_id": tid,
        "tournament_title": tourn.get("title", "Ametheus Tournament"),
        "tournament_slug": tslug,
        "game_id": str(gid) if gid else None,
        "game_slug": game_slug,
        "game_name": game_name,
        "status": calculated_status,
        "format": fmt,
        "series_format": tourn.get("series_format") or tourn.get("match_format") or "BO1",
        "is_battle_royale": is_br,
        "is_solo": bool(tourn.get("is_solo")),

        "current_encounter_id": current_enc_id,
        "current_encounter_number": current_enc_no,
        "current_encounter_title": current_enc_title,
        "current_round_name": current_round_name,
        "current_round_number": current_round_no,
        "current_stage": current_round_name,

        "team_a_id": team_a_id,
        "team_b_id": team_b_id,
        "team_a_name": team_a_name,
        "team_b_name": team_b_name,
        "team_a": team_a_doc,
        "team_b": team_b_doc,
        "score_a": score_a,
        "score_b": score_b,

        "teams": teams_list,
        "team_ids": team_ids_list,
        "squads_count": squads_count,
        "placements": placements,
        "qualifier_team_ids": qualifier_team_ids,
        "winner_team_id": winner_team_id,

        "server_region": server_region,
        "lobby_code": lobby_code,
        "lobby_password": lobby_password,
        "room_id": room_id,
        "room_password": room_password,
        "credentials_released": credentials_released,
        "youtube_live_url": youtube_live_url,
        "live_link": youtube_live_url,
        "scheduled_at": scheduled_at,
        "scheduled_date": scheduled_date,
        "scheduled_time": scheduled_time,
        "custom_instructions": custom_instructions,
        "check_in_status": check_in_status,
        "encounter_history": completed_encounters,
        "updated_at": get_utc_now(),
    }

    # 8. Atomic Upsert into db.live_matches (enforces 1:1 uniqueness per tournament)
    synced_doc = await db.live_matches.find_one_and_update(
        {"tournament_id": tid},
        {
            "$set": live_match_data,
            "$setOnInsert": {
                "id": f"live_{tid}",
                "created_at": get_utc_now(),
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    # 9. Broadcast real-time WebSocket telemetry
    try:
        await ws_manager.broadcast({
            "event": "MATCH_STATUS_CHANGED",
            "tournament_id": tid,
            "live_match_id": str(synced_doc.get("_id", synced_doc.get("id"))),
            "current_encounter_id": current_enc_id,
            "current_encounter_number": current_enc_no,
            "status": calculated_status,
        })
    except Exception:
        pass

    return synced_doc

async def reconcile_all_live_matches(db: AsyncIOMotorDatabase) -> int:
    """
    Audits and synchronizes every tournament into a single Live Match record.
    Enforces uniqueness, eliminates duplicate cards, and preserves all encounter histories.
    """
    await ensure_live_match_indexes(db)

    tourn_cursor = db.tournaments.find({
        "status": {"$nin": ["CANCELLED", "DELETED"]},
        "is_deleted": {"$ne": True},
    })
    tournaments = await tourn_cursor.to_list(length=500)
    reconciled_count = 0
    active_tourn_ids = set()

    for tourn in tournaments:
        tid = str(tourn.get("_id", tourn.get("id")))
        res = await sync_live_match_for_tournament(tid, db)
        if res:
            reconciled_count += 1
            active_tourn_ids.add(tid)

    # Purge any live_matches records that don't have an active encounter or belong to deleted/cancelled tournaments
    await db.live_matches.delete_many({
        "$or": [
            {"tournament_id": {"$nin": list(active_tourn_ids)}},
            {"current_encounter_id": None},
            {"current_encounter_id": {"$exists": False}},
        ]
    })

    return reconciled_count
