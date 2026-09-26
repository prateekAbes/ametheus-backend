import secrets
import json
import math
from typing import List, Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.models.tournament import Tournament, TournamentRegistration
from app.models.team import Team
from app.models.match import Match

def get_round_name(round_num: int, total_rounds: int) -> str:
    diff = total_rounds - round_num
    if diff == 0:
        return "Grand Final"
    elif diff == 1:
        return "Semifinals"
    elif diff == 2:
        return "Quarterfinals"
    elif diff == 3:
        return "Round of 16"
    return f"Round {round_num}"

async def build_single_elimination_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    # 1. Get registered teams or solo contenders
    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })
    regs = await reg_cursor.to_list(length=100)
    team_ids = [str(r.get("team_id")) for r in regs if r.get("team_id")]
    solo_ids = [str(r.get("player_id") or r.get("user_id")) for r in regs if (r.get("player_id") or r.get("user_id")) and not r.get("team_id")]
    
    contenders = []
    if team_ids:
        team_cursor = db.teams.find({"$or": [{"_id": {"$in": team_ids}}, {"id": {"$in": team_ids}}]})
        team_docs = await team_cursor.to_list(length=100)
        team_map = {str(t.get("_id", t.get("id"))): t for t in team_docs}
        for tid in team_ids:
            if tid in team_map:
                contenders.append(tid)
            else:
                contenders.append(tid)
    elif solo_ids:
        user_cursor = db.users.find({"$or": [{"_id": {"$in": solo_ids}}, {"id": {"$in": solo_ids}}]})
        user_docs = await user_cursor.to_list(length=100)
        user_map = {str(u.get("_id", u.get("id"))): u for u in user_docs}
        for uid in solo_ids:
            if uid in user_map:
                contenders.append(uid)
            else:
                contenders.append(uid)

    num_contenders = len(contenders)
    if num_contenders < 2:
        num_contenders = 4

    total_rounds = max(1, math.ceil(math.log2(num_contenders)))

    bracket_tree = []
    bye_matches = []

    # Clear existing matches for this tournament if regenerating
    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    # Generate matches round by round
    for r in range(1, total_rounds + 1):
        r_name = get_round_name(r, total_rounds)
        num_matches = 2 ** (total_rounds - r)
        r_bracket = {"round_number": r, "round_name": r_name, "matches": []}

        for m_idx in range(1, num_matches + 1):
            stage_name = f"{r_name} — Match {m_idx}"
            team_a_id = None
            team_b_id = None
            status = "SCHEDULED"

            if r == 1:
                # Seed contenders into Round 1
                t_idx_a = (m_idx - 1) * 2
                t_idx_b = t_idx_a + 1
                if t_idx_a < len(contenders):
                    team_a_id = contenders[t_idx_a]
                if t_idx_b < len(contenders):
                    team_b_id = contenders[t_idx_b]

                if team_a_id and team_b_id:
                    status = "LIVE"
                elif team_a_id and not team_b_id:
                    status = "BYE"
                else:
                    status = "SCHEDULED"

            lobby_pw = f"AMT-{secrets.token_hex(3).upper()}"
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                round_number=r,
                match_number=m_idx,
                format="Best of 3" if r == total_rounds else "Best of 1",
                scheduled_time=f"Round {r} Schedule",
                status=status,
                team_a_id=team_a_id,
                team_b_id=team_b_id,
                winner_team_id=team_a_id if status == "BYE" else None,
                server_region=tournament.server_region,
                lobby_code=f"AMETHEUS-{tournament.slug[:6].upper()}-R{r}M{m_idx}",
                lobby_password=lobby_pw,
            )
            await db.matches.insert_one(db_match.to_mongo())

            if status == "BYE":
                bye_matches.append(db_match)

            r_bracket["matches"].append({
                "id": db_match.id,
                "stage": stage_name,
                "match_number": m_idx,
                "team_a_id": team_a_id,
                "team_b_id": team_b_id,
                "status": status,
            })

        bracket_tree.append(r_bracket)

    # Auto-advance all BYE contenders into the next round
    for bm in bye_matches:
        if bm.winner_team_id:
            await advance_bracket_match(tournament.id, bm, bm.winner_team_id, db)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "LIVE"}}
    )
    return {"total_rounds": total_rounds, "bracket": bracket_tree}

async def advance_bracket_match(
    tournament_id: str,
    completed_match: Match,
    winner_team_id: str,
    db: AsyncIOMotorDatabase,
) -> Optional[Match]:
    """
    Advances the winner of completed_match into the next round match.
    """
    completed_match.winner_team_id = winner_team_id
    completed_match.status = "COMPLETED"
    await db.matches.update_one(
        {"$or": [{"_id": completed_match.id}, {"id": completed_match.id}]},
        {"$set": {"winner_team_id": winner_team_id, "status": "COMPLETED"}}
    )

    # Find next round match
    next_round_num = completed_match.round_number + 1
    next_match_num = math.ceil(completed_match.match_number / 2)
    is_team_a = (completed_match.match_number % 2) != 0

    next_match_doc = await db.matches.find_one({
        "tournament_id": tournament_id,
        "round_number": next_round_num,
        "match_number": next_match_num,
    })

    next_match = None
    if next_match_doc:
        next_match = Match.from_mongo(next_match_doc)
        update_data = {}
        if is_team_a:
            next_match.team_a_id = winner_team_id
            update_data["team_a_id"] = winner_team_id
        else:
            next_match.team_b_id = winner_team_id
            update_data["team_b_id"] = winner_team_id

        if (update_data.get("team_a_id") or next_match.team_a_id) and (update_data.get("team_b_id") or next_match.team_b_id):
            next_match.status = "LIVE"
            update_data["status"] = "LIVE"

        await db.matches.update_one({"_id": next_match_doc["_id"]}, {"$set": update_data})
    else:
        # If no next match, this was the Grand Final! Tournament completed!
        await db.tournaments.update_one(
            {"$or": [{"_id": tournament_id}, {"id": tournament_id}]},
            {"$set": {"status": "COMPLETED"}}
        )

    return next_match

