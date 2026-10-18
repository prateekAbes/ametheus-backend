from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import uuid
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import HTTPException, status

from app.models.rating import PlayerRating, RatingAuditLog
from app.models.user import User

async def get_or_create_player_rating(
    db: AsyncIOMotorDatabase,
    user_id: str,
    game_id: str,
    game_slug: Optional[str] = None,
) -> dict:
    """
    Retrieves the authoritative rating for a player and game.
    If no record exists, initializes with 500 starting rating.
    """
    query = {
        "user_id": str(user_id),
        "$or": [
            {"game_id": str(game_id)},
            {"game_slug": str(game_slug or game_id)},
        ]
    }
    existing = await db.player_ratings.find_one(query)
    if existing:
        return existing

    # Create new with 500 initial rating
    now = datetime.now(timezone.utc)
    new_doc = {
        "_id": f"pr_{uuid.uuid4().hex[:12]}",
        "user_id": str(user_id),
        "game_id": str(game_id),
        "game_slug": str(game_slug or game_id),
        "rating": 500,
        "highest_rating": 500,
        "is_dummy": False,
        "created_at": now,
        "updated_at": now,
    }
    await db.player_ratings.insert_one(new_doc)
    return new_doc

async def get_player_ratings_for_games(
    db: AsyncIOMotorDatabase,
    user_id: str,
) -> Dict[str, dict]:
    """
    Returns a dictionary of game_id/game_slug -> rating record for a user.
    """
    cursor = db.player_ratings.find({"user_id": str(user_id)})
    docs = await cursor.to_list(100)
    res = {}
    for d in docs:
        if d.get("game_id"):
            res[str(d["game_id"])] = d
        if d.get("game_slug"):
            res[str(d["game_slug"])] = d
    return res

async def apply_manual_rating_adjustment(
    db: AsyncIOMotorDatabase,
    admin_user: User,
    player_id: str,
    game_id: str,
    adjustment: int,
    reason: str,
    explanation: str,
    tournament_id: Optional[str] = None,
    round_id: Optional[str] = None,
    match_id: Optional[str] = None,
    submission_id: Optional[str] = None,
) -> dict:
    """
    Applies an exact manual integer adjustment (+/-) to a player's rating for a specific game.
    Persists immutable audit log record. Idempotent when submission_id is provided.
    """
    # 1. Fetch player user info
    player = await db.users.find_one({
        "$or": [
            {"_id": player_id},
            {"id": player_id},
            {"clerk_user_id": player_id},
        ]
    })
    resolved_player_id = str(player["_id"] if player and "_id" in player else (player["id"] if player and "id" in player else player_id))

    # Idempotency check: if submission_id is provided, check if already recorded
    if submission_id:
        existing_log = await db.rating_audit_logs.find_one({
            "submission_id": str(submission_id),
            "player_id": resolved_player_id,
        })
        if existing_log:
            return existing_log

    # 2. Fetch game info
    game = await db.games.find_one({
        "$or": [
            {"_id": game_id},
            {"id": game_id},
            {"slug": game_id},
        ]
    })
    game_slug = game.get("slug", game_id) if game else game_id
    game_name = game.get("name", game_slug) if game else game_slug
    canonical_game_id = str(game.get("_id", game.get("id", game_slug))) if game else game_slug

    # 3. Get or initialize rating record (starts at 500)
    current_rating_doc = await get_or_create_player_rating(
        db,
        user_id=resolved_player_id,
        game_id=canonical_game_id,
        game_slug=game_slug,
    )
    previous_rating = int(current_rating_doc.get("rating", 500))
    new_rating = max(0, previous_rating + int(adjustment))
    highest_rating = max(int(current_rating_doc.get("highest_rating", 500)), new_rating)

    now = datetime.now(timezone.utc)

    # 4. Update db.player_ratings
    await db.player_ratings.update_one(
        {"_id": current_rating_doc["_id"]},
        {
            "$set": {
                "rating": new_rating,
                "highest_rating": highest_rating,
                "updated_at": now,
            }
        }
    )

    # Also update user's game_ratings dict on db.users
    await db.users.update_one(
        {"$or": [{"_id": resolved_player_id}, {"id": resolved_player_id}]},
        {
            "$set": {
                f"game_ratings.{game_slug}": new_rating,
                "updated_at": now,
            }
        }
    )

    # 5. Tournament & Round metadata
    tournament_name = None
    if tournament_id:
        t_doc = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
        if t_doc:
            tournament_name = t_doc.get("title") or t_doc.get("name")

    round_name = None
    if round_id and tournament_id:
        # Check rounds inside tournament stages
        t_doc = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
        if t_doc:
            for stg in t_doc.get("stages", []):
                for rnd in stg.get("rounds", []):
                    if str(rnd.get("id")) == str(round_id) or str(rnd.get("round_id")) == str(round_id):
                        round_name = rnd.get("name") or rnd.get("round_name")
                        break
                if round_name:
                    break

    # 6. Immutable audit record
    log_doc = {
        "_id": f"ral_{uuid.uuid4().hex[:12]}",
        "id": f"ral_{uuid.uuid4().hex[:12]}",
        "player_id": resolved_player_id,
        "player_username": player.get("username") if player else "",
        "player_display_name": player.get("display_name") if player else "",
        "game_id": canonical_game_id,
        "game_slug": game_slug,
        "game_name": game_name,
        "previous_rating": previous_rating,
        "adjustment": int(adjustment),
        "new_rating": new_rating,
        "reason": reason,
        "explanation": explanation,
        "admin_id": str(getattr(admin_user, "id", "admin")),
        "admin_username": getattr(admin_user, "username", "Admin"),
        "tournament_id": tournament_id,
        "tournament_name": tournament_name,
        "round_id": round_id,
        "round_name": round_name,
        "match_id": match_id,
        "submission_id": str(submission_id) if submission_id else None,
        "is_reversal": False,
        "original_log_id": None,
        "created_at": now,
    }
    await db.rating_audit_logs.insert_one(log_doc)
    return log_doc

async def reverse_rating_adjustment(
    db: AsyncIOMotorDatabase,
    admin_user: User,
    log_id: str,
    reason: Optional[str] = None,
) -> dict:
    """
    Performs an auditable reversal (compensating transaction) for a previous rating adjustment.
    """
    orig = await db.rating_audit_logs.find_one({"$or": [{"_id": log_id}, {"id": log_id}]})
    if not orig:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Original rating adjustment audit record not found",
        )

    if orig.get("is_reversed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This rating adjustment has already been reversed",
        )

    player_id = str(orig["player_id"])
    game_id = str(orig["game_id"])
    game_slug = orig.get("game_slug", game_id)
    orig_adj = int(orig["adjustment"])
    reversal_adj = -orig_adj

    player_rating_doc = await get_or_create_player_rating(db, player_id, game_id, game_slug)
    curr_rating = int(player_rating_doc.get("rating", 500))
    new_rating = max(0, curr_rating + reversal_adj)

    now = datetime.now(timezone.utc)

    # Update player rating
    await db.player_ratings.update_one(
        {"_id": player_rating_doc["_id"]},
        {"$set": {"rating": new_rating, "updated_at": now}}
    )

    await db.users.update_one(
        {"$or": [{"_id": player_id}, {"id": player_id}]},
        {"$set": {f"game_ratings.{game_slug}": new_rating, "updated_at": now}}
    )

    # Insert reversal audit log
    reversal_id = f"ral_rev_{uuid.uuid4().hex[:12]}"
    rev_log = {
        "_id": reversal_id,
        "id": reversal_id,
        "player_id": player_id,
        "player_username": orig.get("player_username", ""),
        "player_display_name": orig.get("player_display_name", ""),
        "game_id": game_id,
        "game_slug": game_slug,
        "game_name": orig.get("game_name", ""),
        "previous_rating": curr_rating,
        "adjustment": reversal_adj,
        "new_rating": new_rating,
        "reason": "Administrative Correction",
        "explanation": reason or f"Compensating reversal of prior adjustment ({orig_adj:+d}) log #{log_id}",
        "admin_id": str(getattr(admin_user, "id", "admin")),
        "admin_username": getattr(admin_user, "username", "Admin"),
        "tournament_id": orig.get("tournament_id"),
        "tournament_name": orig.get("tournament_name"),
        "round_id": orig.get("round_id"),
        "round_name": orig.get("round_name"),
        "match_id": orig.get("match_id"),
        "is_reversal": True,
        "original_log_id": log_id,
        "created_at": now,
    }
    await db.rating_audit_logs.insert_one(rev_log)

    # Mark original log as reversed
    await db.rating_audit_logs.update_one(
        {"_id": orig["_id"]},
        {"$set": {"is_reversed": True, "reversal_log_id": reversal_id, "reversed_at": now}}
    )

    return rev_log

async def get_player_rating_history(
    db: AsyncIOMotorDatabase,
    player_id: str,
    game_id: Optional[str] = None,
    limit: int = 50,
) -> List[dict]:
    """
    Returns player-facing rating audit history.
    """
    q: Dict[str, Any] = {"player_id": str(player_id)}
    if game_id and game_id != "all":
        q["$or"] = [{"game_id": str(game_id)}, {"game_slug": str(game_id)}]
    cursor = db.rating_audit_logs.find(q).sort("created_at", -1).limit(limit)
    return await cursor.to_list(limit)

async def get_rating_management_hierarchy(db: AsyncIOMotorDatabase) -> List[dict]:
    """
    Fetches the full hierarchy for admin rating management:
    Games -> Tournaments -> Rounds -> Matches -> Participants (with current rating & avatar).
    """
    games = await db.games.find({"status": "ACTIVE"}).to_list(100)
    hierarchy = []

    for g in games:
        gid = str(g.get("_id", g.get("id")))
        g_slug = g.get("slug", gid)
        g_name = g.get("name", g_slug)

        tournaments = await db.tournaments.find({
            "$or": [
                {"game_id": gid},
                {"game_id": g_slug},
            ],
            "is_dummy": {"$ne": True},
        }).sort("created_at", -1).to_list(100)

        t_list = []
        for t in tournaments:
            tid = str(t.get("_id", t.get("id")))
            t_title = t.get("title") or t.get("name") or "Tournament"
            t_status = t.get("status", "REGISTRATION_OPEN")

            stages = t.get("stages", [])
            rounds_list = []
            if stages:
                for stg in stages:
                    stg_name = stg.get("name") or stg.get("stage_name") or "Stage"
                    for rnd in stg.get("rounds", []):
                        rid = str(rnd.get("id") or rnd.get("round_id"))
                        r_name = rnd.get("name") or rnd.get("round_name") or f"{stg_name} - Round"
                        rounds_list.append({
                            "round_id": rid,
                            "round_name": r_name,
                            "status": rnd.get("status", "UPCOMING"),
                        })
            else:
                # Flat fallback round if tournament has no stages
                rounds_list.append({
                    "round_id": f"round_default_{tid}",
                    "round_name": "Round 1 (Main Bracket)",
                    "status": "ACTIVE",
                })

            # Fetch matches for this tournament
            matches = await db.matches.find({
                "$or": [
                    {"tournament_id": tid},
                    {"tournament_id": t.get("slug", tid)},
                ],
                "is_dummy": {"$ne": True},
            }).to_list(200)

            matches_by_round = {}
            for m in matches:
                mid = str(m.get("_id", m.get("id")))
                m_round_id = str(m.get("round_id") or "")
                if not m_round_id:
                    # Match to first round or fallback
                    m_round_id = rounds_list[0]["round_id"] if rounds_list else f"round_default_{tid}"

                m_title = f"{m.get('stage', 'Match')} #{m.get('match_number', 1)}"
                if m.get("team_a_name") and m.get("team_b_name"):
                    m_title += f": {m['team_a_name']} vs {m['team_b_name']}"

                # Gather participants for this match
                participants = []
                team_ids = []
                if m.get("team_a_id"):
                    team_ids.append(str(m["team_a_id"]))
                if m.get("team_b_id"):
                    team_ids.append(str(m["team_b_id"]))
                for t_entry in m.get("teams", []):
                    if isinstance(t_entry, dict) and t_entry.get("id"):
                        team_ids.append(str(t_entry["id"]))
                    elif isinstance(t_entry, str):
                        team_ids.append(t_entry)
                for tid_extra in m.get("team_ids", []):
                    if str(tid_extra) not in team_ids:
                        team_ids.append(str(tid_extra))

                # Fetch teams and their player members
                if team_ids:
                    teams_docs = await db.teams.find({
                        "$or": [
                            {"_id": {"$in": team_ids}},
                            {"id": {"$in": team_ids}},
                        ]
                    }).to_list(100)

                    for tm in teams_docs:
                        t_name = tm.get("name", "Team")
                        cap_id = tm.get("captain_id")
                        member_ids = [str(cap_id)] if cap_id else []
                        for p in tm.get("players", []):
                            p_uid = p.get("user_id") if isinstance(p, dict) else str(p)
                            if p_uid and str(p_uid) not in member_ids:
                                member_ids.append(str(p_uid))

                        members_docs = await db.users.find({
                            "$or": [
                                {"_id": {"$in": member_ids}},
                                {"id": {"$in": member_ids}},
                                {"clerk_user_id": {"$in": member_ids}},
                            ]
                        }).to_list(100)

                        for u in members_docs:
                            uid = str(u.get("_id", u.get("id")))
                            pr = await get_or_create_player_rating(db, uid, gid, g_slug)
                            participants.append({
                                "player_id": uid,
                                "username": u.get("username", "player"),
                                "display_name": u.get("display_name") or u.get("username", "Player"),
                                "avatar_url": u.get("avatar_url"),
                                "team_id": str(tm.get("_id", tm.get("id"))),
                                "team_name": t_name,
                                "current_rating": int(pr.get("rating", 500)),
                            })

                # If no teams found (or solo tournament), check tournament registrations
                if not participants:
                    regs = await db.tournament_registrations.find({
                        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug", tid)}],
                        "status": {"$ne": "CANCELLED"},
                    }).to_list(100)
                    uids = [str(r["player_id"]) for r in regs if r.get("player_id")]
                    if uids:
                        users_docs = await db.users.find({
                            "$or": [
                                {"_id": {"$in": uids}},
                                {"id": {"$in": uids}},
                                {"clerk_user_id": {"$in": uids}},
                            ]
                        }).to_list(100)
                        for u in users_docs:
                            uid = str(u.get("_id", u.get("id")))
                            pr = await get_or_create_player_rating(db, uid, gid, g_slug)
                            participants.append({
                                "player_id": uid,
                                "username": u.get("username", "player"),
                                "display_name": u.get("display_name") or u.get("username", "Player"),
                                "avatar_url": u.get("avatar_url"),
                                "team_id": None,
                                "team_name": "Solo Contender",
                                "current_rating": int(pr.get("rating", 500)),
                            })

                match_info = {
                    "match_id": mid,
                    "title": m_title,
                    "status": m.get("status", "SCHEDULED"),
                    "round_id": m_round_id,
                    "participants": participants,
                }
                matches_by_round.setdefault(m_round_id, []).append(match_info)

            # Attach matches to rounds
            for rnd in rounds_list:
                rnd["matches"] = matches_by_round.get(rnd["round_id"], [])

            t_list.append({
                "tournament_id": tid,
                "title": t_title,
                "status": t_status,
                "rounds": rounds_list,
            })

        hierarchy.append({
            "game_id": gid,
            "game_slug": g_slug,
            "game_name": g_name,
            "tournaments": t_list,
        })

    return hierarchy

async def resolve_match_players(
    match: dict,
    db: AsyncIOMotorDatabase,
) -> List[dict]:
    """
    Resolves authentic player identities (user_id, username, display_name, avatar, current MMR)
    for a completed or live match. Supports both team/squad rosters and solo contenders.
    Never assigns team IDs or placeholder hashes as player identities.
    """
    mid = str(match.get("_id", match.get("id", "")))
    tid = str(match.get("tournament_id", ""))
    gid = str(match.get("game_id", ""))
    g_slug = str(match.get("game_slug", gid))

    if not gid or not g_slug:
        if tid:
            t_doc = await db.tournaments.find_one({"$or": [{"_id": tid}, {"id": tid}, {"slug": tid}]})
            if t_doc:
                gid = str(t_doc.get("game_id", gid))
                g_slug = str(t_doc.get("game_slug", gid))

    team_ids: List[str] = []
    if match.get("team_a_id"):
        team_ids.append(str(match["team_a_id"]))
    if match.get("team_b_id"):
        team_ids.append(str(match["team_b_id"]))
    for t_entry in match.get("teams", []) or []:
        if isinstance(t_entry, dict):
            t_id = t_entry.get("id") or t_entry.get("_id") or t_entry.get("team_id")
            if t_id and str(t_id) not in team_ids:
                team_ids.append(str(t_id))
        elif isinstance(t_entry, str) and t_entry not in team_ids:
            team_ids.append(t_entry)
    for tid_extra in match.get("team_ids", []) or []:
        if tid_extra and str(tid_extra) not in team_ids:
            team_ids.append(str(tid_extra))

    players: List[dict] = []
    seen_user_ids = set()

    # 1. Check embedded team details if already present on match
    for team_key, tname_key, tid_val in [
        ("team_a_details", "team_a_name", match.get("team_a_id")),
        ("team_b_details", "team_b_name", match.get("team_b_id")),
    ]:
        details = match.get(team_key)
        if isinstance(details, dict) and details.get("members"):
            t_name = details.get("name") or match.get(tname_key) or "Team"
            for mem in details["members"]:
                uid = str(mem.get("id") or mem.get("user_id") or "")
                if uid and uid not in seen_user_ids:
                    seen_user_ids.add(uid)
                    pr = await get_or_create_player_rating(db, uid, gid, g_slug) if gid else {"rating": 500}
                    players.append({
                        "player_id": uid,
                        "user_id": uid,
                        "username": mem.get("name") or mem.get("username") or "Player",
                        "display_name": mem.get("name") or mem.get("display_name") or mem.get("username") or "Player",
                        "avatar_url": mem.get("avatar_url") or mem.get("avatar"),
                        "team_id": str(tid_val) if tid_val else None,
                        "team_name": t_name,
                        "current_rating": int(pr.get("rating", 500)),
                        "match_id": mid,
                    })

    # 2. Query db.teams for teams
    if team_ids:
        teams_docs = await db.teams.find({
            "$or": [
                {"_id": {"$in": team_ids}},
                {"id": {"$in": team_ids}},
            ]
        }).to_list(100)

        team_dict = {str(tm.get("_id", tm.get("id"))): tm for tm in teams_docs}
        all_member_ids = []
        user_to_team = {}

        for tm in teams_docs:
            t_id = str(tm.get("_id", tm.get("id")))
            cap_id = tm.get("captain_id")
            if cap_id:
                s_cap = str(cap_id)
                all_member_ids.append(s_cap)
                user_to_team[s_cap] = tm
            for p in tm.get("players", []):
                p_uid = p.get("user_id") if isinstance(p, dict) else str(p)
                if p_uid:
                    s_p = str(p_uid)
                    if s_p not in all_member_ids:
                        all_member_ids.append(s_p)
                    user_to_team[s_p] = tm

        if all_member_ids:
            users_docs = await db.users.find({
                "$or": [
                    {"_id": {"$in": all_member_ids}},
                    {"id": {"$in": all_member_ids}},
                    {"clerk_user_id": {"$in": all_member_ids}},
                ]
            }).to_list(200)

            for u in users_docs:
                uid = str(u.get("_id", u.get("id")))
                if uid not in seen_user_ids:
                    seen_user_ids.add(uid)
                    tm = user_to_team.get(uid) or user_to_team.get(u.get("clerk_user_id", ""))
                    t_name = tm.get("name", "Team") if tm else "Team"
                    t_id = str(tm.get("_id", tm.get("id"))) if tm else None
                    pr = await get_or_create_player_rating(db, uid, gid, g_slug) if gid else {"rating": 500}
                    players.append({
                        "player_id": uid,
                        "user_id": uid,
                        "username": u.get("username", "player"),
                        "display_name": u.get("display_name") or u.get("username", "Player"),
                        "avatar_url": u.get("avatar_url"),
                        "team_id": t_id,
                        "team_name": t_name,
                        "current_rating": int(pr.get("rating", 500)),
                        "match_id": mid,
                    })

    # 3. If no team players found or solo contenders, check registrations or users directly
    if not players:
        regs = []
        if tid:
            regs = await db.tournament_registrations.find({
                "$or": [{"tournament_id": tid}],
                "status": {"$ne": "CANCELLED"},
            }).to_list(100)

        uids = [str(r["player_id"]) for r in regs if r.get("player_id")]
        # Also check direct user_ids or player_ids on match
        for m_uid in match.get("player_ids", []) or match.get("user_ids", []) or []:
            if str(m_uid) not in uids:
                uids.append(str(m_uid))

        if uids:
            users_docs = await db.users.find({
                "$or": [
                    {"_id": {"$in": uids}},
                    {"id": {"$in": uids}},
                    {"clerk_user_id": {"$in": uids}},
                ]
            }).to_list(100)

            for u in users_docs:
                uid = str(u.get("_id", u.get("id")))
                if uid not in seen_user_ids:
                    seen_user_ids.add(uid)
                    pr = await get_or_create_player_rating(db, uid, gid, g_slug) if gid else {"rating": 500}
                    players.append({
                        "player_id": uid,
                        "user_id": uid,
                        "username": u.get("username", "player"),
                        "display_name": u.get("display_name") or u.get("username", "Player"),
                        "avatar_url": u.get("avatar_url"),
                        "team_id": None,
                        "team_name": "Solo Contender",
                        "current_rating": int(pr.get("rating", 500)),
                        "match_id": mid,
                    })

    return players
