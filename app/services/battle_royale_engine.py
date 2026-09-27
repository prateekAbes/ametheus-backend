import math
from typing import Optional, List, Dict, Any, Tuple
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.models.match import Match
from app.models.base import generate_uuid, get_utc_now

BATTLE_ROYALE_GAME_SPECS: Dict[str, Dict[str, int]] = {
    "bgmi": {"room_capacity": 100, "players_per_team": 4},
    "battlegrounds-mobile-india": {"room_capacity": 100, "players_per_team": 4},
    "pubg": {"room_capacity": 100, "players_per_team": 4},
    "free-fire": {"room_capacity": 48, "players_per_team": 4},
    "free-fire-max": {"room_capacity": 48, "players_per_team": 4},
    "call-of-duty-mobile": {"room_capacity": 100, "players_per_team": 4},
    "codm": {"room_capacity": 100, "players_per_team": 4},
    "apex-legends": {"room_capacity": 60, "players_per_team": 3},
    "fortnite": {"room_capacity": 100, "players_per_team": 4},
    "smash-karts": {"room_capacity": 12, "players_per_team": 3},
}

def get_battle_royale_specs(
    game_slug_or_name: str = "",
    tournament_doc: Optional[Dict[str, Any]] = None,
    game_doc: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int]:
    """
    Returns (room_capacity, players_per_team).
    Uses tournament/game overrides if set, else falls back to canonical game defaults.
    """
    clean_slug = (game_slug_or_name or "").lower().strip()

    # 1. Base defaults from canonical game dictionary
    base_room = 100
    base_team_size = 4
    for key, spec in BATTLE_ROYALE_GAME_SPECS.items():
        if key in clean_slug:
            base_room = spec["room_capacity"]
            base_team_size = spec["players_per_team"]
            break

    # 2. Check game_doc overrides
    if game_doc:
        g_room = game_doc.get("room_capacity")
        if g_room and int(g_room) > 0:
            base_room = int(g_room)
        g_min = game_doc.get("min_team_size")
        g_max = game_doc.get("max_team_size")
        if g_min and g_max and g_min == g_max and int(g_min) > 0:
            base_team_size = int(g_min)

    # 3. Check tournament overrides
    if tournament_doc:
        t_room = tournament_doc.get("room_capacity")
        if t_room and int(t_room) > 0:
            base_room = int(t_room)
        t_players = tournament_doc.get("players_per_team")
        if t_players and int(t_players) > 0:
            base_team_size = int(t_players)
        else:
            t_min = tournament_doc.get("min_team_size")
            t_max = tournament_doc.get("max_team_size")
            if t_min and t_max and t_min == t_max and int(t_min) > 0:
                base_team_size = int(t_min)

    return max(1, base_room), max(1, base_team_size)


def calculate_battle_royale_parameters(
    actual_registered_teams: int,
    room_capacity: int,
    players_per_team: int,
) -> Dict[str, int]:
    """
    Authoritative Battle Royale calculation:
    Maximum Teams Per Match = floor(Maximum Players Per Room / Active Players Per Team)
    Required Matches = ceil(Actual Registered Teams / Maximum Teams Per Match)
    """
    active_ppt = max(1, players_per_team)
    max_room = max(1, room_capacity)
    max_teams_per_match = max(1, math.floor(max_room / active_ppt))
    
    if actual_registered_teams <= 0:
        required_matches = 0
    else:
        required_matches = math.ceil(actual_registered_teams / max_teams_per_match)

    return {
        "actual_registered_teams": actual_registered_teams,
        "room_capacity": max_room,
        "players_per_team": active_ppt,
        "max_teams_per_match": max_teams_per_match,
        "required_matches": required_matches,
    }


def distribute_teams_into_matches(
    teams: List[Dict[str, Any]],
    max_teams_per_match: int,
    required_matches: int,
    balanced: bool = False,
) -> List[List[Dict[str, Any]]]:
    """
    Distributes actual registered teams into match lobbies.
    If balanced: evenly distributes teams across matches (e.g. 51 teams -> 17, 17, 17).
    If not balanced: fills sequentially up to max_teams_per_match (e.g. 51 teams -> 25, 25, 1).
    """
    total_teams = len(teams)
    if total_teams == 0 or required_matches == 0:
        return []

    if balanced:
        # Distribute teams round-robin or chunk evenly
        distribution: List[List[Dict[str, Any]]] = [[] for _ in range(required_matches)]
        for idx, team in enumerate(teams):
            match_idx = idx % required_matches
            distribution[match_idx].append(team)
        return distribution

    # Sequential greedy distribution
    distribution = []
    for i in range(required_matches):
        start_idx = i * max_teams_per_match
        end_idx = min(total_teams, (i + 1) * max_teams_per_match)
        distribution.append(teams[start_idx:end_idx])
    return distribution


async def get_confirmed_registered_teams(
    tournament_id: str,
    db: AsyncIOMotorDatabase,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Retrieves the tournament and all confirmed, valid registered teams from the database.
    Filters out REJECTED, CANCELLED, and REFUNDED registrations.
    Does not create dummy teams or count invalid records.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
    if not tourn:
        return None, []

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    tid_query: List[Any] = [tid, tslug]
    try:
        if ObjectId.is_valid(tid):
            tid_query.append(ObjectId(tid))
    except Exception:
        pass

    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": {"$in": tid_query}}, {"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })
    registrations = await reg_cursor.to_list(length=1000)

    teams_list: List[Dict[str, Any]] = []
    seen_ids = set()

    for reg in registrations:
        team_id = reg.get("team_id")
        player_id = reg.get("player_id") or reg.get("user_id")

        if team_id:
            t_id_str = str(team_id)
            if t_id_str in seen_ids:
                continue

            team_or: List[Any] = [{"_id": team_id}, {"id": team_id}]
            try:
                if ObjectId.is_valid(t_id_str):
                    team_or.append({"_id": ObjectId(t_id_str)})
            except Exception:
                pass
            team_doc = await db.teams.find_one({"$or": team_or})
            if team_doc:
                canonical_team_id = str(team_doc.get("_id", team_doc.get("id")))
                seen_ids.add(t_id_str)
                seen_ids.add(canonical_team_id)

                # Captain lookup
                cap_user = None
                cap_id = team_doc.get("captain_id")
                if cap_id:
                    cap_or: List[Any] = [{"_id": cap_id}, {"id": cap_id}]
                    try:
                        if ObjectId.is_valid(str(cap_id)):
                            cap_or.append({"_id": ObjectId(str(cap_id))})
                    except Exception:
                        pass
                    cap_user = await db.users.find_one({"$or": cap_or})

                cap_username = (
                    cap_user.get("username")
                    or cap_user.get("display_name")
                    or team_doc.get("captain_username", "Captain")
                    if cap_user
                    else team_doc.get("captain_username", "Captain")
                )
                cap_game_id = cap_user.get("game_id") if cap_user else None

                # Members list
                members_list = []
                members_handles = []
                for m in team_doc.get("members", []):
                    if isinstance(m, dict):
                        u_id = str(m.get("user_id", ""))
                        u_name = m.get("username", "Player")
                        g_handle = m.get("game_handle", m.get("game_id", ""))
                        role = m.get("role", "PLAYER")
                    else:
                        u_id = str(getattr(m, "user_id", ""))
                        u_name = getattr(m, "username", "Player")
                        g_handle = getattr(m, "game_handle", getattr(m, "game_id", ""))
                        role = getattr(m, "role", "PLAYER")
                    members_list.append({
                        "user_id": u_id,
                        "username": u_name,
                        "game_id": g_handle,
                        "role": role,
                    })
                    members_handles.append(g_handle or u_name)

                if cap_id and not any(m["user_id"] == str(cap_id) for m in members_list):
                    members_list.insert(0, {
                        "user_id": str(cap_id),
                        "username": cap_username,
                        "game_id": cap_game_id or cap_username,
                        "role": "CAPTAIN",
                    })
                    members_handles.insert(0, cap_game_id or cap_username)

                entry = {
                    "id": canonical_team_id,
                    "team_id": canonical_team_id,
                    "name": team_doc.get("name", "Team"),
                    "team_name": team_doc.get("name", "Team"),
                    "tag": team_doc.get("tag", "TEAM"),
                    "team_tag": team_doc.get("tag", "TEAM"),
                    "captain": {
                        "user_id": str(cap_id or ""),
                        "username": cap_username,
                        "game_id": cap_game_id,
                    },
                    "captain_username": cap_username,
                    "members": members_list,
                    "member_handles": members_handles,
                    "is_solo": False,
                }
                teams_list.append(entry)

        elif player_id:
            u_id_str = str(player_id)
            if u_id_str in seen_ids:
                continue

            user_or: List[Any] = [{"_id": player_id}, {"id": player_id}]
            try:
                if ObjectId.is_valid(u_id_str):
                    user_or.append({"_id": ObjectId(u_id_str)})
            except Exception:
                pass
            user_doc = await db.users.find_one({"$or": user_or})
            if user_doc:
                canonical_user_id = str(user_doc.get("_id", user_doc.get("id")))
                seen_ids.add(u_id_str)
                seen_ids.add(canonical_user_id)

                p_name = user_doc.get("display_name") or user_doc.get("username", "Contender")
                g_id = user_doc.get("game_id", "")
                entry = {
                    "id": canonical_user_id,
                    "team_id": canonical_user_id,
                    "name": p_name,
                    "team_name": p_name,
                    "tag": "SOLO",
                    "team_tag": "SOLO",
                    "captain": {
                        "user_id": canonical_user_id,
                        "username": p_name,
                        "game_id": g_id,
                    },
                    "captain_username": p_name,
                    "members": [{
                        "user_id": canonical_user_id,
                        "username": p_name,
                        "game_id": g_id,
                        "role": "SOLO",
                    }],
                    "member_handles": [p_name],
                    "is_solo": True,
                }
                teams_list.append(entry)

    return tourn, teams_list
