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

def resolve_stage_format(tournament: Tournament, stage_name: str, r: int, total_rounds: int) -> str:
    stage_formats = getattr(tournament, "stage_formats", None) or {}
    if isinstance(stage_formats, dict):
        if stage_name in stage_formats:
            return stage_formats[stage_name]
        r_name = get_round_name(r, total_rounds)
        if r_name in stage_formats:
            return stage_formats[r_name]

    default_fmt = getattr(tournament, "match_format", None) or getattr(tournament, "format", None)
    if default_fmt and default_fmt in ["Best of 1", "Best of 3", "Best of 5"]:
        return default_fmt
    return "Best of 3" if r == total_rounds else "Best of 1"

async def get_contenders(tournament: Tournament, db: AsyncIOMotorDatabase) -> List[str]:
    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    regs = await reg_cursor.to_list(length=200)
    team_ids = [str(r.get("team_id")) for r in regs if r.get("team_id")]
    solo_ids = [str(r.get("player_id") or r.get("user_id")) for r in regs if (r.get("player_id") or r.get("user_id")) and not r.get("team_id")]

    contenders = []
    if team_ids:
        team_cursor = db.teams.find({"$or": [{"_id": {"$in": team_ids}}, {"id": {"$in": team_ids}}]})
        team_docs = await team_cursor.to_list(length=200)
        team_map = {str(t.get("_id", t.get("id"))): t for t in team_docs}
        for tid in team_ids:
            if tid in team_map or tid:
                contenders.append(tid)
    elif solo_ids:
        for uid in solo_ids:
            if uid:
                contenders.append(uid)
    return contenders

async def build_single_elimination_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    contenders = await get_contenders(tournament, db)
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
            fmt = resolve_stage_format(tournament, stage_name, r, total_rounds)

            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                round_number=r,
                match_number=m_idx,
                format=fmt,
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

    for bm in bye_matches:
        if bm.winner_team_id:
            await advance_bracket_match(tournament.id, bm, bm.winner_team_id, db)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "LIVE"}}
    )
    return {"total_rounds": total_rounds, "bracket": bracket_tree}

async def build_double_elimination_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    """
    Constructs a full Double Elimination tree: Upper Bracket, Lower Bracket, and Grand Final.
    """
    contenders = await get_contenders(tournament, db)
    num_contenders = max(4, len(contenders))
    upper_rounds = max(1, math.ceil(math.log2(num_contenders)))

    bracket_tree = []
    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    # Upper Bracket
    for r in range(1, upper_rounds + 1):
        num_matches = 2 ** (upper_rounds - r)
        r_name = f"Upper Bracket Round {r}" if r < upper_rounds else "Upper Bracket Final"
        r_bracket = {"round_number": r, "round_name": r_name, "matches": []}

        for m_idx in range(1, num_matches + 1):
            stage_name = f"{r_name} — Match {m_idx}"
            team_a_id = contenders[(m_idx - 1) * 2] if r == 1 and (m_idx - 1) * 2 < len(contenders) else None
            team_b_id = contenders[(m_idx - 1) * 2 + 1] if r == 1 and (m_idx - 1) * 2 + 1 < len(contenders) else None
            status = "LIVE" if team_a_id and team_b_id else "SCHEDULED"

            fmt = resolve_stage_format(tournament, stage_name, r, upper_rounds + 2)
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                round_number=r,
                match_number=m_idx,
                format=fmt,
                scheduled_time=f"Upper R{r}",
                status=status,
                team_a_id=team_a_id,
                team_b_id=team_b_id,
                server_region=tournament.server_region,
                lobby_code=f"AMT-UB-R{r}M{m_idx}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": status})

        bracket_tree.append(r_bracket)

    # Lower Bracket Rounds
    lower_rounds = max(1, (upper_rounds - 1) * 2)
    for lr in range(1, lower_rounds + 1):
        stage_name = f"Lower Bracket Round {lr}" if lr < lower_rounds else "Lower Bracket Final"
        lr_bracket = {"round_number": upper_rounds + lr, "round_name": stage_name, "matches": []}
        db_match = Match(
            tournament_id=tournament.id,
            stage=stage_name,
            round_number=upper_rounds + lr,
            match_number=1,
            format=resolve_stage_format(tournament, stage_name, upper_rounds + lr, upper_rounds + lower_rounds + 1),
            scheduled_time=f"Lower R{lr}",
            status="SCHEDULED",
            server_region=tournament.server_region,
            lobby_code=f"AMT-LB-R{lr}",
            lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
        )
        await db.matches.insert_one(db_match.to_mongo())
        lr_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": "SCHEDULED"})
        bracket_tree.append(lr_bracket)

    # Grand Final
    gf_stage = "Grand Final (Double Elimination)"
    gf_match = Match(
        tournament_id=tournament.id,
        stage=gf_stage,
        round_number=upper_rounds + lower_rounds + 1,
        match_number=1,
        format=resolve_stage_format(tournament, gf_stage, upper_rounds + lower_rounds + 1, upper_rounds + lower_rounds + 1),
        scheduled_time="Grand Final Showdown",
        status="SCHEDULED",
        server_region=tournament.server_region,
        lobby_code="AMT-GF-01",
        lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
    )
    await db.matches.insert_one(gf_match.to_mongo())
    bracket_tree.append({"round_number": upper_rounds + lower_rounds + 1, "round_name": "Grand Final", "matches": [{"id": gf_match.id, "stage": gf_stage, "status": "SCHEDULED"}]})

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "LIVE"}}
    )
    return {"total_rounds": len(bracket_tree), "bracket": bracket_tree}

async def build_round_robin_schedule(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    """
    Constructs a Round Robin schedule where each contender plays against every other contender.
    """
    contenders = await get_contenders(tournament, db)
    n = len(contenders)
    if n < 2:
        return {"status": "error", "message": "At least 2 contenders required for Round Robin."}

    # Berger tables / round-robin pairing algorithm
    teams = list(contenders)
    if n % 2 != 0:
        teams.append(None) # Bye dummy
        n += 1

    total_rounds = n - 1
    matches_per_round = n // 2
    bracket_tree = []

    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    fmt = getattr(tournament, "match_format", None) or "Best of 1"

    for r in range(total_rounds):
        r_num = r + 1
        r_name = f"Round {r_num} (League Stage)"
        r_bracket = {"round_number": r_num, "round_name": r_name, "matches": []}

        for m_idx in range(matches_per_round):
            t1 = teams[m_idx]
            t2 = teams[n - 1 - m_idx]

            if t1 is None or t2 is None:
                continue # Bye match

            stage_name = f"{r_name} — Fixture {m_idx + 1}"
            status = "LIVE" if r_num == 1 else "SCHEDULED"

            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                round_number=r_num,
                match_number=m_idx + 1,
                format=fmt,
                scheduled_time=f"Round {r_num} Fixture",
                status=status,
                team_a_id=t1,
                team_b_id=t2,
                server_region=tournament.server_region,
                lobby_code=f"AMT-RR-R{r_num}M{m_idx+1}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": status})

        # Rotate teams keeping teams[0] fixed
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
        bracket_tree.append(r_bracket)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "LIVE"}}
    )
    return {"total_rounds": total_rounds, "bracket": bracket_tree}

async def build_swiss_pairings(
    tournament: Tournament,
    round_number: int,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    """
    Pairs contenders based on matching or adjacent match record points.
    """
    contenders = await get_contenders(tournament, db)
    if len(contenders) < 2:
        return {"status": "error", "message": "At least 2 contenders required for Swiss."}

    # Fetch match history to calculate win-loss points
    history_matches = await db.matches.find({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}],
        "status": "COMPLETED",
    }).to_list(length=500)

    points = {c: 0 for c in contenders}
    for m in history_matches:
        w = m.get("winner_team_id")
        if w in points:
            points[w] += 1

    # Sort contenders descending by win points
    sorted_contenders = sorted(contenders, key=lambda c: points[c], reverse=True)
    paired = []
    r_name = f"Swiss Round {round_number}"
    r_bracket = {"round_number": round_number, "round_name": r_name, "matches": []}
    fmt = getattr(tournament, "match_format", None) or "Best of 1"

    m_idx = 1
    i = 0
    while i < len(sorted_contenders) - 1:
        t_a = sorted_contenders[i]
        t_b = sorted_contenders[i + 1]
        stage_name = f"{r_name} — Table {m_idx}"

        db_match = Match(
            tournament_id=tournament.id,
            stage=stage_name,
            round_number=round_number,
            match_number=m_idx,
            format=fmt,
            scheduled_time=f"Swiss Round {round_number}",
            status="LIVE" if round_number == 1 else "SCHEDULED",
            team_a_id=t_a,
            team_b_id=t_b,
            server_region=tournament.server_region,
            lobby_code=f"AMT-SW-R{round_number}T{m_idx}",
            lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
        )
        await db.matches.insert_one(db_match.to_mongo())
        r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": db_match.status})
        m_idx += 1
        i += 2

    # If odd, last contender gets BYE
    if i < len(sorted_contenders):
        bye_cand = sorted_contenders[i]
        bye_match = Match(
            tournament_id=tournament.id,
            stage=f"{r_name} — Table {m_idx} (BYE)",
            round_number=round_number,
            match_number=m_idx,
            format=fmt,
            scheduled_time=f"Swiss Round {round_number}",
            status="BYE",
            team_a_id=bye_cand,
            winner_team_id=bye_cand,
            server_region=tournament.server_region,
            lobby_code=f"AMT-SW-BYE",
            lobby_password=f"AMT-BYE",
        )
        await db.matches.insert_one(bye_match.to_mongo())
        r_bracket["matches"].append({"id": bye_match.id, "stage": bye_match.stage, "status": "BYE"})

    return {"round_number": round_number, "bracket": [r_bracket]}

async def generate_tournament_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    """
    Main dispatch engine: generates the appropriate bracket based on tournament_type.
    """
    ttype = (getattr(tournament, "tournament_type", None) or getattr(tournament, "format", "")).lower()

    if "double" in ttype:
        return await build_double_elimination_bracket(tournament, db)
    elif "round" in ttype or "robin" in ttype or "league" in ttype:
        return await build_round_robin_schedule(tournament, db)
    elif "swiss" in ttype:
        return await build_swiss_pairings(tournament, 1, db)
    else:
        # Default: Single Elimination
        return await build_single_elimination_bracket(tournament, db)

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
        # If no next match in this branch, check if all tournament matches completed
        remaining = await db.matches.count_documents({
            "tournament_id": tournament_id,
            "status": {"$in": ["LIVE", "SCHEDULED", "CHECK_IN_OPEN", "PENDING_VERIFICATION", "DISPUTED"]}
        })
        if remaining == 0:
            await db.tournaments.update_one(
                {"$or": [{"_id": tournament_id}, {"id": tournament_id}]},
                {"$set": {"status": "COMPLETED"}}
            )

    return next_match
