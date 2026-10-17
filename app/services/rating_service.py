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
) -> dict:
    """
    Applies an exact manual integer adjustment (+/-) to a player's rating for a specific game.
    Persists immutable audit log record.
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
