import secrets
import json
import math
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import HTTPException
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

    for attr in ["match_format", "series_format", "format"]:
        default_fmt = getattr(tournament, attr, None)
        if default_fmt in ["Best of 1", "Best of 3"]:
            return default_fmt
        if default_fmt in ["BO1", "BO3"]:
            return default_fmt
    return "BO3" if r == total_rounds else "BO1"

async def get_contenders(tournament: Tournament, db: AsyncIOMotorDatabase) -> List[str]:
    from bson import ObjectId
    tid_query = [tournament.id, tournament.slug]
    try:
        if ObjectId.is_valid(str(tournament.id)):
            tid_query.append(ObjectId(str(tournament.id)))
    except Exception:
        pass

    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": {"$in": tid_query}}, {"tournament_id": tournament.id}, {"tournament_id": tournament.slug}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })
    regs = await reg_cursor.to_list(length=200)
    team_ids = [str(r.get("team_id")) for r in regs if r.get("team_id")]
    solo_ids = [str(r.get("player_id") or r.get("user_id")) for r in regs if (r.get("player_id") or r.get("user_id")) and not r.get("team_id")]

    contenders = []
    if team_ids:
        team_or = []
        for tid in team_ids:
            team_or.append({"_id": tid})
            team_or.append({"id": tid})
            try:
                if ObjectId.is_valid(tid):
                    team_or.append({"_id": ObjectId(tid)})
            except Exception:
                pass
        team_cursor = db.teams.find({"$or": team_or}) if team_or else None
        team_docs = await team_cursor.to_list(length=200) if team_cursor else []
        team_map = {}
        for t in team_docs:
            team_map[str(t.get("_id"))] = t
            if t.get("id"):
                team_map[str(t.get("id"))] = t
        for tid in team_ids:
            if tid in team_map:
                contenders.append(str(team_map[tid].get("_id", team_map[tid].get("id"))))
            else:
                contenders.append(tid)
    elif solo_ids:
        for uid in solo_ids:
            if uid:
                contenders.append(uid)
    return contenders


# 1. SINGLE ELIMINATION
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

    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

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

                if team_a_id and not team_b_id:
                    status = "BYE"
                else:
                    status = "SCHEDULED"

            lobby_pw = f"AMT-{secrets.token_hex(3).upper()}"
            fmt = resolve_stage_format(tournament, stage_name, r, total_rounds)

            match_type = "GRAND_FINAL" if r == total_rounds else ("SEMI_FINAL" if r == total_rounds - 1 else ("QUARTER_FINAL" if r == total_rounds - 2 else "ROUND_OF_16"))

            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type=match_type,
                round_number=r,
                match_number=m_idx,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"Round {r} Match {m_idx}",
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
                "match_type": match_type,
                "match_number": m_idx,
                "team_a_id": team_a_id,
                "team_b_id": team_b_id,
                "status": status,
            })

        bracket_tree.append(r_bracket)

    for bm in bye_matches:
        if bm.winner_team_id:
            await advance_bracket_match(tournament.id, bm, bm.winner_team_id, db)

    # Rule 5 & 11: Tournament NEVER has LIVE status. It remains REGISTRATION_CLOSED.
    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": total_rounds, "bracket": bracket_tree}

# 2. DOUBLE ELIMINATION
async def build_double_elimination_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
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
            status = "SCHEDULED"

            fmt = resolve_stage_format(tournament, stage_name, r, upper_rounds + 2)
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type="WINNERS_BRACKET",
                round_number=r,
                match_number=m_idx,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"UB Round {r}",
                status=status,
                team_a_id=team_a_id,
                team_b_id=team_b_id,
                server_region=tournament.server_region,
                lobby_code=f"AMT-UB-R{r}M{m_idx}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "match_type": "WINNERS_BRACKET", "status": status})

        bracket_tree.append(r_bracket)

    # Lower Bracket
    lower_rounds = max(1, (upper_rounds - 1) * 2)
    for lr in range(1, lower_rounds + 1):
        r_name = f"Lower Bracket Round {lr}" if lr < lower_rounds else "Lower Bracket Final"
        r_bracket = {"round_number": upper_rounds + lr, "round_name": r_name, "matches": []}
        num_matches = max(1, 2 ** max(0, upper_rounds - 1 - math.ceil(lr / 2)))

        for m_idx in range(1, num_matches + 1):
            stage_name = f"{r_name} — Match {m_idx}"
            fmt = resolve_stage_format(tournament, stage_name, upper_rounds + lr, upper_rounds + lower_rounds + 1)
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type="LOSERS_BRACKET",
                round_number=upper_rounds + lr,
                match_number=m_idx,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"LB Round {lr}",
                status="SCHEDULED",
                server_region=tournament.server_region,
                lobby_code=f"AMT-LB-R{lr}M{m_idx}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "match_type": "LOSERS_BRACKET", "status": "SCHEDULED"})

        bracket_tree.append(r_bracket)

    # Grand Final
    gf_stage = "Grand Final Championship"
    gf_fmt = resolve_stage_format(tournament, gf_stage, upper_rounds + lower_rounds + 1, upper_rounds + lower_rounds + 1)
    gf_match = Match(
        tournament_id=tournament.id,
        stage=gf_stage,
        match_type="GRAND_FINAL",
        round_number=upper_rounds + lower_rounds + 1,
        match_number=1,
        format=gf_fmt,
        series_format=gf_fmt,
        scheduled_time="Grand Final Showdown",
        status="SCHEDULED",
        server_region=tournament.server_region,
        lobby_code="AMT-GF-01",
        lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
    )
    await db.matches.insert_one(gf_match.to_mongo())
    bracket_tree.append({"round_number": upper_rounds + lower_rounds + 1, "round_name": "Grand Final", "matches": [{"id": gf_match.id, "stage": gf_stage, "match_type": "GRAND_FINAL", "status": "SCHEDULED"}]})

    # Rule 5 & 11: Tournament NEVER has LIVE status.
    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": len(bracket_tree), "bracket": bracket_tree}

# 3. CONSOLATION / FEED-IN
async def build_consolation_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    contenders = await get_contenders(tournament, db)
    num_contenders = max(4, len(contenders))
    total_rounds = max(1, math.ceil(math.log2(num_contenders)))

    bracket_tree = []
    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    # Main Championship Bracket
    for r in range(1, total_rounds + 1):
        r_name = f"Main {get_round_name(r, total_rounds)}"
        num_matches = 2 ** (total_rounds - r)
        r_bracket = {"round_number": r, "round_name": r_name, "matches": []}

        for m_idx in range(1, num_matches + 1):
            stage_name = f"{r_name} — Match {m_idx}"
            team_a_id = contenders[(m_idx - 1) * 2] if r == 1 and (m_idx - 1) * 2 < len(contenders) else None
            team_b_id = contenders[(m_idx - 1) * 2 + 1] if r == 1 and (m_idx - 1) * 2 + 1 < len(contenders) else None
            status = "SCHEDULED"

            fmt = resolve_stage_format(tournament, stage_name, r, total_rounds)
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type="FINAL" if r == total_rounds else "SEMI_FINAL",
                round_number=r,
                match_number=m_idx,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"Main Round {r}",
                status=status,
                team_a_id=team_a_id,
                team_b_id=team_b_id,
                server_region=tournament.server_region,
                lobby_code=f"AMT-MAIN-R{r}M{m_idx}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": status})

        bracket_tree.append(r_bracket)

    # Consolation Bracket (for 3rd Place / 1st Round Feed-in)
    consolation_rounds = max(1, total_rounds - 1)
    for cr in range(1, consolation_rounds + 1):
        cr_name = f"Consolation Plate Round {cr}" if cr < consolation_rounds else "Consolation Final"
        cr_bracket = {"round_number": total_rounds + cr, "round_name": cr_name, "matches": []}
        num_matches = max(1, 2 ** (consolation_rounds - cr))

        for m_idx in range(1, num_matches + 1):
            stage_name = f"{cr_name} — Match {m_idx}"
            fmt = resolve_stage_format(tournament, stage_name, total_rounds + cr, total_rounds + consolation_rounds)
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type="THIRD_PLACE",
                round_number=total_rounds + cr,
                match_number=m_idx,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"Consolation Round {cr}",
                status="SCHEDULED",
                server_region=tournament.server_region,
                lobby_code=f"AMT-CONS-R{cr}M{m_idx}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            cr_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": "SCHEDULED"})

        bracket_tree.append(cr_bracket)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": len(bracket_tree), "bracket": bracket_tree}

# 4. SINGLE ROUND ROBIN
async def build_round_robin_schedule(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    contenders = await get_contenders(tournament, db)
    n = len(contenders)
    if n < 2:
        return {"status": "error", "message": "At least 2 contenders required for Round Robin."}

    teams = list(contenders)
    if n % 2 != 0:
        teams.append(None)
        n += 1

    total_rounds = n - 1
    matches_per_round = n // 2
    bracket_tree = []

    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    fmt = getattr(tournament, "series_format", None) or "BO1"

    for r in range(total_rounds):
        r_num = r + 1
        r_name = f"Round {r_num} (League Fixtures)"
        r_bracket = {"round_number": r_num, "round_name": r_name, "matches": []}

        for m_idx in range(matches_per_round):
            t1 = teams[m_idx]
            t2 = teams[n - 1 - m_idx]

            if t1 is None or t2 is None:
                continue

            stage_name = f"{r_name} — Fixture {m_idx + 1}"
            status = "SCHEDULED"

            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type="GROUP_STAGE",
                round_number=r_num,
                match_number=m_idx + 1,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"Round {r_num} Fixture {m_idx + 1}",
                status=status,
                team_a_id=t1,
                team_b_id=t2,
                server_region=tournament.server_region,
                lobby_code=f"AMT-RR-R{r_num}M{m_idx+1}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": status})

        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
        bracket_tree.append(r_bracket)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": total_rounds, "bracket": bracket_tree}

# 5. DOUBLE ROUND ROBIN
async def build_double_round_robin_schedule(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    first_leg = await build_round_robin_schedule(tournament, db)
    bracket_tree = first_leg.get("bracket", [])
    total_r1 = len(bracket_tree)

    # Leg 2: Reverse home and away
    leg1_matches = await db.matches.find({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    }).to_list(length=200)

    fmt = getattr(tournament, "series_format", None) or "BO1"
    leg2_tree = []

    for m in leg1_matches:
        r2_num = m["round_number"] + total_r1
        stage_name = f"Round {r2_num} (Reverse Fixtures) — Fixture {m['match_number']}"
        db_match = Match(
            tournament_id=tournament.id,
            stage=stage_name,
            match_type="GROUP_STAGE",
            round_number=r2_num,
            match_number=m["match_number"],
            format=fmt,
            series_format=fmt,
            scheduled_time=f"Round {r2_num} Fixture",
            status="SCHEDULED",
            team_a_id=m.get("team_b_id"),
            team_b_id=m.get("team_a_id"),
            server_region=tournament.server_region,
            lobby_code=f"AMT-DRR-R{r2_num}M{m['match_number']}",
            lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
        )
        await db.matches.insert_one(db_match.to_mongo())

    full_bracket = await db.matches.find({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    }).to_list(length=300)

    # Group by round
    rounds_map = {}
    for m in full_bracket:
        rn = m["round_number"]
        if rn not in rounds_map:
            rounds_map[rn] = {"round_number": rn, "round_name": f"Round {rn}", "matches": []}
        rounds_map[rn]["matches"].append({"id": str(m.get("_id", m.get("id"))), "stage": m["stage"], "status": m["status"]})

    final_tree = [rounds_map[k] for k in sorted(rounds_map.keys())]
    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": final_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": len(final_tree), "bracket": final_tree}

# 6. SWISS SYSTEM
async def build_swiss_pairings(
    tournament: Tournament,
    round_number: int,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    contenders = await get_contenders(tournament, db)
    if len(contenders) < 2:
        return {"status": "error", "message": "At least 2 contenders required for Swiss."}

    # Fetch prior match scores to compute records
    completed = await db.matches.find({
        "tournament_id": tournament.id,
        "status": "COMPLETED",
    }).to_list(length=200)

    points = {c: 0 for c in contenders}
    for m in completed:
        w = m.get("winner_team_id")
        if w in points:
            points[w] += 3

    # Sort contenders by points
    ranked = sorted(contenders, key=lambda c: points.get(c, 0), reverse=True)

    r_name = f"Swiss Round {round_number}"
    r_bracket = {"round_number": round_number, "round_name": r_name, "matches": []}
    fmt = resolve_stage_format(tournament, r_name, round_number, 5)

    pairs = []
    i = 0
    while i < len(ranked) - 1:
        pairs.append((ranked[i], ranked[i + 1]))
        i += 2

    odd_contender = ranked[-1] if len(ranked) % 2 != 0 else None

    for m_idx, (t_a, t_b) in enumerate(pairs, start=1):
        stage_name = f"{r_name} — Table {m_idx}"
        db_match = Match(
            tournament_id=tournament.id,
            stage=stage_name,
            match_type="SWISS_ROUND",
            round_number=round_number,
            match_number=m_idx,
            format=fmt,
            series_format=fmt,
            scheduled_time=f"Swiss Round {round_number} Match {m_idx}",
            status="SCHEDULED",
            team_a_id=t_a,
            team_b_id=t_b,
            server_region=tournament.server_region,
            lobby_code=f"AMT-SW-R{round_number}M{m_idx}",
            lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
        )
        await db.matches.insert_one(db_match.to_mongo())
        r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": "SCHEDULED"})

    if odd_contender:
        bye_match = Match(
            tournament_id=tournament.id,
            stage=f"{r_name} — Bye",
            match_type="SWISS_ROUND",
            round_number=round_number,
            match_number=len(pairs) + 1,
            format=fmt,
            series_format=fmt,
            scheduled_time=f"Swiss Round {round_number} Bye",
            status="BYE",
            team_a_id=odd_contender,
            winner_team_id=odd_contender,
            server_region=tournament.server_region,
            lobby_code=f"AMT-SW-BYE",
            lobby_password=f"AMT-BYE",
        )
        await db.matches.insert_one(bye_match.to_mongo())
        r_bracket["matches"].append({"id": bye_match.id, "stage": bye_match.stage, "status": "BYE"})

    return {"round_number": round_number, "bracket": [r_bracket]}

# 7. LADDER / PYRAMID
async def build_ladder_structure(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    contenders = await get_contenders(tournament, db)
    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    fmt = getattr(tournament, "series_format", None) or "BO1"
    bracket_tree = []

    # Build rungs: 1 at top (championship rung), 2 at next, 3, 4, etc.
    rungs = []
    idx = 0
    rung_level = 1
    while idx < len(contenders):
        rung_size = rung_level
        rung_teams = contenders[idx:idx + rung_size]
        rungs.append((rung_level, rung_teams))
        idx += rung_size
        rung_level += 1

    matches_list = []
    # Create challenge fixtures within each tier/rung
    for r_level, r_teams in rungs:
        r_bracket = {"round_number": r_level, "round_name": f"Ladder Rung Tier {r_level}", "matches": []}
        for i in range(0, len(r_teams) - 1, 2):
            t_a = r_teams[i]
            t_b = r_teams[i + 1]
            stage_name = f"Rung {r_level} Challenge Match {(i // 2) + 1}"
            db_match = Match(
                tournament_id=tournament.id,
                stage=stage_name,
                match_type="PRELIMINARY",
                round_number=r_level,
                match_number=(i // 2) + 1,
                format=fmt,
                series_format=fmt,
                scheduled_time=f"Rung {r_level} Challenge",
                status="SCHEDULED",
                team_a_id=t_a,
                team_b_id=t_b,
                server_region=tournament.server_region,
                lobby_code=f"AMT-LAD-R{r_level}M{(i // 2) + 1}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_match.to_mongo())
            r_bracket["matches"].append({"id": db_match.id, "stage": stage_name, "status": "SCHEDULED"})
        bracket_tree.append(r_bracket)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": len(bracket_tree), "bracket": bracket_tree}

# 8. GROUP STAGE -> KNOCKOUT
async def build_group_stage_knockout(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    contenders = await get_contenders(tournament, db)
    await db.matches.delete_many({
        "$or": [{"tournament_id": tournament.id}, {"tournament_id": tournament.slug}]
    })

    fmt = getattr(tournament, "series_format", None) or "BO1"
    bracket_tree = []

    # Divide contenders into Group A and Group B
    group_a = contenders[0::2]
    group_b = contenders[1::2]

    # Group A Fixtures
    r_a = {"round_number": 1, "round_name": "Group A Stage", "matches": []}
    for i in range(len(group_a) - 1):
        for j in range(i + 1, len(group_a)):
            m_stage = f"Group A — Match {len(r_a['matches']) + 1}"
            db_m = Match(
                tournament_id=tournament.id,
                stage=m_stage,
                match_type="GROUP_STAGE",
                round_number=1,
                match_number=len(r_a["matches"]) + 1,
                format=fmt,
                series_format=fmt,
                scheduled_time="Group A Schedule",
                status="SCHEDULED",
                team_a_id=group_a[i],
                team_b_id=group_a[j],
                server_region=tournament.server_region,
                lobby_code=f"AMT-GA-M{len(r_a['matches']) + 1}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_m.to_mongo())
            r_a["matches"].append({"id": db_m.id, "stage": m_stage, "status": "SCHEDULED"})
    bracket_tree.append(r_a)

    # Group B Fixtures
    r_b = {"round_number": 2, "round_name": "Group B Stage", "matches": []}
    for i in range(len(group_b) - 1):
        for j in range(i + 1, len(group_b)):
            m_stage = f"Group B — Match {len(r_b['matches']) + 1}"
            db_m = Match(
                tournament_id=tournament.id,
                stage=m_stage,
                match_type="GROUP_STAGE",
                round_number=2,
                match_number=len(r_b["matches"]) + 1,
                format=fmt,
                series_format=fmt,
                scheduled_time="Group B Schedule",
                status="SCHEDULED",
                team_a_id=group_b[i],
                team_b_id=group_b[j],
                server_region=tournament.server_region,
                lobby_code=f"AMT-GB-M{len(r_b['matches']) + 1}",
                lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
            )
            await db.matches.insert_one(db_m.to_mongo())
            r_b["matches"].append({"id": db_m.id, "stage": m_stage, "status": "SCHEDULED"})
    bracket_tree.append(r_b)

    # Knockout Semifinals & Final
    r_ko = {"round_number": 3, "round_name": "Knockout Semifinals", "matches": []}
    for sf in [1, 2]:
        sf_stage = f"Semifinal {sf} (Group Qualifier)"
        db_sf = Match(
            tournament_id=tournament.id,
            stage=sf_stage,
            match_type="SEMI_FINAL",
            round_number=3,
            match_number=sf,
            format="BO3",
            series_format="BO3",
            scheduled_time=f"Semifinal {sf}",
            status="SCHEDULED",
            server_region=tournament.server_region,
            lobby_code=f"AMT-KO-SF{sf}",
            lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
        )
        await db.matches.insert_one(db_sf.to_mongo())
        r_ko["matches"].append({"id": db_sf.id, "stage": sf_stage, "status": "SCHEDULED"})
    bracket_tree.append(r_ko)

    # Championship Final
    r_fn = {"round_number": 4, "round_name": "Knockout Championship Final", "matches": []}
    db_fn = Match(
        tournament_id=tournament.id,
        stage="Championship Grand Final",
        match_type="GRAND_FINAL",
        round_number=4,
        match_number=1,
        format="BO3",
        series_format="BO3",
        scheduled_time="Championship Final",
        status="SCHEDULED",
        server_region=tournament.server_region,
        lobby_code="AMT-KO-FINAL",
        lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
    )
    await db.matches.insert_one(db_fn.to_mongo())
    r_fn["matches"].append({"id": db_fn.id, "stage": "Championship Grand Final", "status": "SCHEDULED"})
    bracket_tree.append(r_fn)

    await db.tournaments.update_one(
        {"$or": [{"_id": tournament.id}, {"id": tournament.id}]},
        {"$set": {"bracket_rounds": bracket_tree, "status": "REGISTRATION_CLOSED"}}
    )
    return {"total_rounds": len(bracket_tree), "bracket": bracket_tree}

# MAIN DISPATCH ROUTER (All 8 Tournament Formats)
async def generate_tournament_bracket(
    tournament: Tournament,
    db: AsyncIOMotorDatabase,
) -> Dict[str, Any]:
    """
    Main dispatch engine: generates match structure for all 8 competitive formats:
    - Single Elimination
    - Double Elimination
    - Consolation / Feed-in
    - Single Round Robin
    - Double Round Robin
    - Swiss System
    - Ladder / Pyramid
    - Group Stage → Single Elimination
    """
    type_fmt = (getattr(tournament, "tournament_type", None) or "").lower()
    match_fmt = (getattr(tournament, "match_format", None) or "").lower()
    format_fmt = (getattr(tournament, "format", None) or "").lower()
    raw_fmt = f"{type_fmt} {match_fmt} {format_fmt}"

    if "double" in raw_fmt and "robin" in raw_fmt:
        return await build_double_round_robin_schedule(tournament, db)
    elif "double" in raw_fmt or "double_elimination" in raw_fmt:
        return await build_double_elimination_bracket(tournament, db)
    elif "consolation" in raw_fmt or "feed" in raw_fmt:
        return await build_consolation_bracket(tournament, db)
    elif "group" in raw_fmt or "knockout" in raw_fmt:
        return await build_group_stage_knockout(tournament, db)
    elif "single_round_robin" in raw_fmt or "round robin" in raw_fmt or "round_robin" in raw_fmt or "robin" in raw_fmt:
        return await build_round_robin_schedule(tournament, db)
    elif "swiss" in raw_fmt:
        return await build_swiss_pairings(tournament, 1, db)
    elif "ladder" in raw_fmt or "pyramid" in raw_fmt:
        return await build_ladder_structure(tournament, db)
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
    Advances the winner of completed_match into the downstream match slot.
    Supports:
    - Explicit destination mapping via next_match_id and next_match_slot ('A' or 'B')
    - Fallback automatic pairing: Round N Match M -> Round N+1 Match ceil(M/2) (odd -> Slot A, even -> Slot B)
    - Non-sequential match completion (e.g. M3 finishes before M1; downstream slot populates immediately)
    - Safe result editing (updates downstream slot if downstream match hasn't started; rejects with 400 if downstream match is already LIVE/COMPLETED)
    - Idempotent execution
    """
    completed_match.winner_team_id = winner_team_id
    completed_match.status = "COMPLETED"
    await db.matches.update_one(
        {"$or": [{"_id": completed_match.id}, {"id": completed_match.id}]},
        {"$set": {"winner_team_id": winner_team_id, "status": "COMPLETED"}}
    )

    # 1. Resolve winner display name
    winner_name = None
    if winner_team_id == completed_match.team_a_id:
        winner_name = completed_match.team_a_name
    elif winner_team_id == completed_match.team_b_id:
        winner_name = completed_match.team_b_name

    if not winner_name:
        team_doc = await db.teams.find_one({"$or": [{"_id": winner_team_id}, {"id": winner_team_id}]})
        if team_doc:
            winner_name = team_doc.get("name")
        else:
            user_doc = await db.users.find_one({"$or": [{"_id": winner_team_id}, {"id": winner_team_id}]})
            if user_doc:
                winner_name = user_doc.get("username")
            elif winner_team_id in [completed_match.team_a_name, completed_match.team_a_placeholder]:
                winner_name = completed_match.team_a_name or completed_match.team_a_placeholder
            elif winner_team_id in [completed_match.team_b_name, completed_match.team_b_placeholder]:
                winner_name = completed_match.team_b_name or completed_match.team_b_placeholder

    if not winner_name:
        winner_name = str(winner_team_id)

    # 2. Determine downstream match and slot
    next_match_doc = None
    is_team_a = True

    # Case A: Explicit next_match_id
    if completed_match.next_match_id:
        next_match_doc = await db.matches.find_one({
            "$or": [{"_id": completed_match.next_match_id}, {"id": completed_match.next_match_id}]
        })
        if next_match_doc:
            slot = (completed_match.next_match_slot or "A").upper()
            is_team_a = (slot != "B")

    # Case B: Standard round-advancement pair
    if not next_match_doc:
        next_round_num = (completed_match.round_number or 1) + 1
        m_num = completed_match.match_number or 1
        next_match_num = math.ceil(m_num / 2)
        is_team_a = (m_num % 2) != 0

        tourn_id_str = str(tournament_id)
        next_match_doc = await db.matches.find_one({
            "$or": [{"tournament_id": tourn_id_str}, {"tournament_id": tournament_id}],
            "round_number": next_round_num,
            "match_number": next_match_num,
        })

        if not next_match_doc and completed_match.round_number:
            tourn_doc = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
            c_rounds = tourn_doc.get("custom_rounds") or [] if tourn_doc else []
            r_label = f"Round {next_round_num}"
            if next_round_num <= len(c_rounds):
                r_label = c_rounds[next_round_num - 1]
            else:
                r_label = get_round_name(next_round_num, max(next_round_num + 1, 3))

            feeder_other = (m_num + 1) if (m_num % 2 != 0) else (m_num - 1)
            new_downstream_match = Match(
                tournament_id=str(tournament_id),
                round_name=r_label,
                round_number=next_round_num,
                match_number=next_match_num,
                stage=r_label,
                match_type="MATCH",
                format="BO3" if "final" in r_label.lower() else "BO1",
                series_format="BO3" if "final" in r_label.lower() else "BO1",
                status="SCHEDULED",
                roster_status="PUBLISHED",
                team_a_id=winner_team_id if is_team_a else None,
                team_a_name=winner_name if is_team_a else None,
                team_a_placeholder=None if is_team_a else f"Winner of Match {feeder_other}",
                team_b_id=winner_team_id if not is_team_a else None,
                team_b_name=winner_name if not is_team_a else None,
                team_b_placeholder=None if not is_team_a else f"Winner of Match {feeder_other}",
                previous_match_ids=[completed_match.id],
            )
            await db.matches.insert_one(new_downstream_match.to_mongo())
            next_match_doc = await db.matches.find_one({"_id": new_downstream_match.id})

    next_match = None
    if next_match_doc:
        next_match = Match.from_mongo(next_match_doc)

        # Safety Check for Result Editing:
        # If downstream match is already active (LIVE, COMPLETED, PENDING_VERIFICATION) or has scores:
        is_downstream_active = (
            next_match.status in ["LIVE", "COMPLETED", "PENDING_VERIFICATION"] or
            (next_match.score_a is not None and next_match.score_a > 0) or
            (next_match.score_b is not None and next_match.score_b > 0) or
            (next_match.winner_team_id is not None)
        )
        existing_occupant = next_match.team_a_id if is_team_a else next_match.team_b_id

        if is_downstream_active and existing_occupant and existing_occupant != winner_team_id:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change winner of Match {completed_match.match_number}: downstream match ({next_match.stage or next_match.round_name}) is already in progress or completed. Reset downstream match result first."
            )

        update_data = {}
        if is_team_a:
            next_match.team_a_id = winner_team_id
            next_match.team_a_name = winner_name
            next_match.team_a_placeholder = None
            update_data["team_a_id"] = winner_team_id
            update_data["team_a_name"] = winner_name
            update_data["team_a_placeholder"] = None
        else:
            next_match.team_b_id = winner_team_id
            next_match.team_b_name = winner_name
            next_match.team_b_placeholder = None
            update_data["team_b_id"] = winner_team_id
            update_data["team_b_name"] = winner_name
            update_data["team_b_placeholder"] = None

        if next_match.status != "LIVE":
            next_match.status = "SCHEDULED"
            update_data["status"] = "SCHEDULED"

        if update_data:
            await db.matches.update_one({"_id": next_match_doc["_id"]}, {"$set": update_data})
        await db.matches.update_one({"_id": next_match_doc["_id"]}, {"$addToSet": {"previous_match_ids": completed_match.id}})

        # Link completed match to next match
        await db.matches.update_one(
            {"$or": [{"_id": completed_match.id}, {"id": completed_match.id}]},
            {"$set": {
                "next_match_id": str(next_match_doc.get("_id", next_match_doc.get("id"))),
                "next_match_slot": "A" if is_team_a else "B",
                "advancement_label": f"{next_match.round_name or f'Round {next_round_num}'} Match {next_match.match_number or next_match_num} Slot {'A' if is_team_a else 'B'}"
            }}
        )

        # If downstream match is published, notify the newly advanced winner
        if getattr(next_match, "roster_status", "PUBLISHED") in ["PUBLISHED", "UPDATED"]:
            try:
                from app.services.match_notification_service import notify_roster_published
                await notify_roster_published(next_match.id, db)
            except Exception as e:
                print(f"[Advanced Team Notification Warning]: {e}")

        # Synchronize bracket_rounds if stored on tournament
        try:
            tourn_doc = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}]})
            if tourn_doc and "bracket_rounds" in tourn_doc and isinstance(tourn_doc["bracket_rounds"], list):
                updated_rounds = tourn_doc["bracket_rounds"]
                target_mid = str(next_match_doc.get("_id", next_match_doc.get("id")))
                modified = False
                for r in updated_rounds:
                    for m in r.get("matches", []):
                        if str(m.get("id")) == target_mid or str(m.get("_id")) == target_mid:
                            if is_team_a:
                                m["team_a_id"] = winner_team_id
                                m["team_a_name"] = winner_name
                                m["team_a_placeholder"] = None
                            else:
                                m["team_b_id"] = winner_team_id
                                m["team_b_name"] = winner_name
                                m["team_b_placeholder"] = None
                            modified = True
                if modified:
                    await db.tournaments.update_one(
                        {"_id": tourn_doc["_id"]},
                        {"$set": {"bracket_rounds": updated_rounds}}
                    )
        except Exception as e:
            print(f"[Tournament Bracket Update Warning]: {e}")

    else:
        # Check if all tournament matches are completed
        tourn_id_str = str(tournament_id)
        remaining = await db.matches.count_documents({
            "$or": [{"tournament_id": tourn_id_str}, {"tournament_id": tournament_id}],
            "status": {"$in": ["LIVE", "SCHEDULED", "CHECK_IN_OPEN", "PENDING_VERIFICATION", "DISPUTED"]}
        })
        if remaining == 0:
            await db.tournaments.update_one(
                {"$or": [{"_id": tournament_id}, {"id": tournament_id}]},
                {"$set": {"status": "COMPLETED"}}
            )

    return next_match


async def swap_match_teams(
    match_id: str,
    db: AsyncIOMotorDatabase
) -> Dict[str, Any]:
    """
    Swaps Team 1 and Team 2 in a match:
    team_a_id <-> team_b_id
    team_a_name <-> team_b_name
    team_a_score <-> team_b_score
    """
    match_query = {"$or": [{"id": match_id}, {"_id": match_id}]}
    if ObjectId.is_valid(match_id):
        match_query["$or"].append({"_id": ObjectId(match_id)})

    match_doc = await db.matches.find_one(match_query)
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    old_a_id = match_doc.get("team_a_id")
    old_b_id = match_doc.get("team_b_id")
    old_a_name = match_doc.get("team_a_name")
    old_b_name = match_doc.get("team_b_name")
    old_a_score = match_doc.get("score_a", 0) or 0
    old_b_score = match_doc.get("score_b", 0) or 0

    update_fields = {
        "team_a_id": old_b_id,
        "team_b_id": old_a_id,
        "team_a_name": old_b_name,
        "team_b_name": old_a_name,
        "score_a": old_b_score,
        "score_b": old_a_score,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    await db.matches.update_one({"_id": match_doc["_id"]}, {"$set": update_fields})
    updated_doc = await db.matches.find_one({"_id": match_doc["_id"]})
    return Match.from_mongo(updated_doc).model_dump()


async def calculate_advancement_suggestion(
    match_id: str,
    db: AsyncIOMotorDatabase
) -> Dict[str, Any]:
    """
    Calculates suggested next stage, round, match, and slot for winner advancement,
    and returns all available destinations across stages/rounds/matches in the tournament.
    """
    match_query = {"$or": [{"id": match_id}, {"_id": match_id}]}
    if ObjectId.is_valid(match_id):
        match_query["$or"].append({"_id": ObjectId(match_id)})

    match_doc = await db.matches.find_one(match_query)
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    tournament_id = str(match_doc.get("tournament_id"))
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = tournament.stages or []
    current_stage_id = match_doc.get("stage_id")
    current_round_id = match_doc.get("round_id")
    match_num = match_doc.get("match_number", 1)

    suggested: Optional[Dict[str, Any]] = None

    # Retrieve all matches of this tournament to build destination tree
    all_matches_cursor = db.matches.find({"tournament_id": tournament_id})
    all_matches_raw = await all_matches_cursor.to_list(length=500)
    all_matches = [Match.from_mongo(m) for m in all_matches_raw]

    # Map matches by stage and round
    matches_by_stage_round: Dict[str, Dict[str, List[Match]]] = {}
    for m in all_matches:
        s_id = m.stage_id or "default"
        r_id = m.round_id or str(m.round_number)
        if s_id not in matches_by_stage_round:
            matches_by_stage_round[s_id] = {}
        if r_id not in matches_by_stage_round[s_id]:
            matches_by_stage_round[s_id][r_id] = []
        matches_by_stage_round[s_id][r_id].append(m)

    # Sort stages by order
    sorted_stages = sorted(stages, key=lambda s: s.stage_order)

    # Try finding next round in current stage, or first round in next stage
    current_stage_idx = -1
    for idx, s in enumerate(sorted_stages):
        if s.stage_id == current_stage_id:
            current_stage_idx = idx
            break

    target_stage = None
    target_round = None

    if current_stage_idx >= 0:
        c_stage = sorted_stages[current_stage_idx]
        c_rounds = sorted(c_stage.rounds or [], key=lambda r: r.round_order)
        c_round_idx = -1
        for r_idx, r in enumerate(c_rounds):
            if r.round_id == current_round_id:
                c_round_idx = r_idx
                break

        if c_round_idx >= 0 and c_round_idx + 1 < len(c_rounds):
            # Next round in current stage
            target_stage = c_stage
            target_round = c_rounds[c_round_idx + 1]
        elif current_stage_idx + 1 < len(sorted_stages):
            # First round in next stage
            next_stage = sorted_stages[current_stage_idx + 1]
            if next_stage.rounds:
                target_stage = next_stage
                target_round = sorted(next_stage.rounds, key=lambda r: r.round_order)[0]

    # Find suggested match and slot in target round
    if target_stage and target_round:
        target_stage_matches = matches_by_stage_round.get(target_stage.stage_id, {}).get(target_round.round_id, [])
        target_stage_matches = sorted(target_stage_matches, key=lambda m: m.match_number)

        suggested_match_num = math.ceil(match_num / 2)
        suggested_slot = "team_a" if (match_num % 2 != 0) else "team_b"

        # Try to find match matching suggested match number
        target_match = None
        for m in target_stage_matches:
            if m.match_number == suggested_match_num:
                target_match = m
                break

        # Fallback to any match with an empty slot
        if not target_match:
            for m in target_stage_matches:
                if (suggested_slot == "team_a" and not m.team_a_id) or (suggested_slot == "team_b" and not m.team_b_id):
                    target_match = m
                    break

        if not target_match and target_stage_matches:
            target_match = target_stage_matches[0]

        suggested = {
            "stage_id": target_stage.stage_id,
            "stage_name": target_stage.stage_name,
            "round_id": target_round.round_id,
            "round_name": target_round.round_name,
            "match_id": target_match.id if target_match else None,
            "match_number": target_match.match_number if target_match else suggested_match_num,
            "slot": suggested_slot,
        }
    else:
        # Fallback for standard single-stage tournaments
        next_round_num = match_doc.get("round_number", 1) + 1
        suggested_match_num = math.ceil(match_num / 2)
        suggested_slot = "team_a" if (match_num % 2 != 0) else "team_b"
        target_match = None
        for m in all_matches:
            if m.round_number == next_round_num and m.match_number == suggested_match_num:
                target_match = m
                break
        suggested = {
            "stage_id": current_stage_id or "default",
            "stage_name": match_doc.get("stage") or "Tournament Stage",
            "round_id": str(next_round_num),
            "round_name": f"Round {next_round_num}",
            "match_id": target_match.id if target_match else None,
            "match_number": suggested_match_num,
            "slot": suggested_slot,
        }

    # Build available destinations list for admin manual selection
    destinations = []
    if sorted_stages:
        for s in sorted_stages:
            stage_entry = {
                "stage_id": s.stage_id,
                "stage_name": s.stage_name,
                "stage_order": s.stage_order,
                "rounds": []
            }
            for r in sorted(s.rounds or [], key=lambda r: r.round_order):
                round_entry = {
                    "round_id": r.round_id,
                    "round_name": r.round_name,
                    "round_order": r.round_order,
                    "matches": []
                }
                r_matches = matches_by_stage_round.get(s.stage_id, {}).get(r.round_id, [])
                for m in sorted(r_matches, key=lambda m: m.match_number):
                    # Do not include the source match itself as a destination
                    if m.id != match_id:
                        round_entry["matches"].append({
                            "match_id": m.id,
                            "match_number": m.match_number,
                            "team_a_id": m.team_a_id,
                            "team_a_name": m.team_a_name,
                            "team_b_id": m.team_b_id,
                            "team_b_name": m.team_b_name,
                            "status": m.status
                        })
                stage_entry["rounds"].append(round_entry)
            destinations.append(stage_entry)

    # Current match details
    source_match = Match.from_mongo(match_doc).model_dump()

    return {
        "source_match": source_match,
        "suggested": suggested,
        "available_destinations": destinations
    }


async def advance_match_winner(
    match_id: str,
    payload: Dict[str, Any],
    db: AsyncIOMotorDatabase
) -> Dict[str, Any]:
    """
    Advances a match winner to the target stage/round/match/slot.
    Updates target match slot, updates source match status & advancement trail.
    """
    match_query = {"$or": [{"id": match_id}, {"_id": match_id}]}
    if ObjectId.is_valid(match_id):
        match_query["$or"].append({"_id": ObjectId(match_id)})

    match_doc = await db.matches.find_one(match_query)
    if not match_doc:
        raise HTTPException(status_code=404, detail="Source match not found")

    winner_team_id = payload.get("winner_team_id") or match_doc.get("winner_team_id")
    if not winner_team_id:
        raise HTTPException(status_code=400, detail="A winner team must be specified before advancing")

    # Fetch team name
    winner_team_name = "Team"
    team_query = {"$or": [{"id": winner_team_id}, {"_id": winner_team_id}]}
    if ObjectId.is_valid(winner_team_id):
        team_query["$or"].append({"_id": ObjectId(winner_team_id)})
    t_doc = await db.teams.find_one(team_query)
    if t_doc:
        winner_team_name = t_doc.get("name", "Team")
    elif winner_team_id == match_doc.get("team_a_id"):
        winner_team_name = match_doc.get("team_a_name", "Team A")
    elif winner_team_id == match_doc.get("team_b_id"):
        winner_team_name = match_doc.get("team_b_name", "Team B")

    target_match_id = payload.get("destination_match_id") or payload.get("target_match_id")
    target_stage_id = payload.get("destination_stage_id") or payload.get("target_stage_id")
    target_round_id = payload.get("destination_round_id") or payload.get("target_round_id")
    target_slot = payload.get("destination_slot") or payload.get("target_slot") or "team_a"

    if not target_match_id:
        raise HTTPException(status_code=400, detail="Target destination match ID is required")

    dest_query = {"$or": [{"id": target_match_id}, {"_id": target_match_id}]}
    if ObjectId.is_valid(target_match_id):
        dest_query["$or"].append({"_id": ObjectId(target_match_id)})

    dest_doc = await db.matches.find_one(dest_query)
    if not dest_doc:
        raise HTTPException(status_code=404, detail="Destination match not found")

    # Update destination match slot
    dest_update: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if target_slot == "team_a":
        dest_update["team_a_id"] = winner_team_id
        dest_update["team_a_name"] = winner_team_name
    else:
        dest_update["team_b_id"] = winner_team_id
        dest_update["team_b_name"] = winner_team_name

    await db.matches.update_one({"_id": dest_doc["_id"]}, {"$set": dest_update})

    # Update source match with advancement metadata
    advancement_data = {
        "advanced_team_id": winner_team_id,
        "advanced_team_name": winner_team_name,
        "target_stage_id": target_stage_id,
        "target_round_id": target_round_id,
        "target_match_id": target_match_id,
        "target_slot": target_slot,
        "advanced_at": datetime.now(timezone.utc).isoformat()
    }

    source_update = {
        "winner_team_id": winner_team_id,
        "status": "COMPLETED",
        "advancement": advancement_data,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.matches.update_one({"_id": match_doc["_id"]}, {"$set": source_update})

    updated_source = await db.matches.find_one({"_id": match_doc["_id"]})
    updated_dest = await db.matches.find_one({"_id": dest_doc["_id"]})

    return {
        "success": True,
        "message": f"Successfully advanced {winner_team_name} to destination match",
        "source_match": Match.from_mongo(updated_source).model_dump(),
        "destination_match": Match.from_mongo(updated_dest).model_dump(),
        "advancement": advancement_data
    }


async def revert_match_advancement(
    match_id: str,
    db: AsyncIOMotorDatabase
) -> Dict[str, Any]:
    """
    Reverts an advancement, safely clearing the advanced team from the destination match slot
    and resetting the source match advancement metadata.
    """
    match_query = {"$or": [{"id": match_id}, {"_id": match_id}]}
    if ObjectId.is_valid(match_id):
        match_query["$or"].append({"_id": ObjectId(match_id)})

    match_doc = await db.matches.find_one(match_query)
    if not match_doc:
        raise HTTPException(status_code=404, detail="Source match not found")

    advancement = match_doc.get("advancement")
    if not advancement:
        raise HTTPException(status_code=400, detail="No advancement recorded for this match")

    target_match_id = advancement.get("target_match_id")
    target_slot = advancement.get("target_slot", "team_a")

    if target_match_id:
        dest_query = {"$or": [{"id": target_match_id}, {"_id": target_match_id}]}
        if ObjectId.is_valid(target_match_id):
            dest_query["$or"].append({"_id": ObjectId(target_match_id)})

        dest_doc = await db.matches.find_one(dest_query)
        if dest_doc:
            dest_update: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
            if target_slot == "team_a":
                dest_update["team_a_id"] = None
                dest_update["team_a_name"] = None
            else:
                dest_update["team_b_id"] = None
                dest_update["team_b_name"] = None
            await db.matches.update_one({"_id": dest_doc["_id"]}, {"$set": dest_update})

    # Clear advancement from source match
    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$unset": {"advancement": ""}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}}
    )

    updated_source = await db.matches.find_one({"_id": match_doc["_id"]})
    return {
        "success": True,
        "message": "Advancement reverted successfully",
        "source_match": Match.from_mongo(updated_source).model_dump()
    }


async def get_tournament_history(
    tournament_id: str,
    db: AsyncIOMotorDatabase
) -> Dict[str, Any]:
    """
    Returns full tournament history stage-by-stage and round-by-round,
    preserving scores, winners, dates/times, stream URLs, and advancement paths.
    """
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)

    matches_cursor = db.matches.find({"tournament_id": tournament_id}).sort([("round_number", 1), ("match_number", 1)])
    raw_matches = await matches_cursor.to_list(length=500)
    all_matches = [Match.from_mongo(m) for m in raw_matches]

    # Structure by stages
    stages_history = []
    if tournament.stages:
        sorted_stages = sorted(tournament.stages, key=lambda s: s.stage_order)
        for s in sorted_stages:
            stage_entry = {
                "stage_id": s.stage_id,
                "stage_name": s.stage_name,
                "stage_order": s.stage_order,
                "stage_format": s.stage_format,
                "rounds": []
            }
            for r in sorted(s.rounds or [], key=lambda r: r.round_order):
                r_matches = [
                    m.model_dump() for m in all_matches
                    if (m.stage_id == s.stage_id and m.round_id == r.round_id)
                ]
                stage_entry["rounds"].append({
                    "round_id": r.round_id,
                    "round_name": r.round_name,
                    "round_order": r.round_order,
                    "series_format": r.series_format,
                    "matches": r_matches
                })
            stages_history.append(stage_entry)

    return {
        "tournament_id": tournament.id,
        "tournament_name": getattr(tournament, "name", None) or getattr(tournament, "title", "Tournament"),
        "tournament_status": tournament.status,
        "live_stream_url": tournament.live_stream_url,
        "stages": stages_history,
        "matches": [m.model_dump() for m in all_matches]
    }


async def get_team_journey(
    tournament_id: str,
    team_id: str,
    db: AsyncIOMotorDatabase
) -> Dict[str, Any]:
    """
    Returns the dynamic Team Journey path across all stages and rounds for a given team.
    """
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)

    team_query = {"$or": [{"id": team_id}, {"_id": team_id}]}
    if ObjectId.is_valid(team_id):
        team_query["$or"].append({"_id": ObjectId(team_id)})
    team_doc = await db.teams.find_one(team_query)
    team_name = team_doc.get("name", "Team") if team_doc else "Team"

    # Find matches where team participated
    matches_cursor = db.matches.find({
        "tournament_id": tournament_id,
        "$or": [
            {"team_a_id": team_id},
            {"team_b_id": team_id}
        ]
    }).sort([("round_number", 1), ("match_number", 1)])
    raw_matches = await matches_cursor.to_list(length=100)

    journey_steps = []
    for m_doc in raw_matches:
        m = Match.from_mongo(m_doc)
        is_team_a = (m.team_a_id == team_id)
        opponent_id = m.team_b_id if is_team_a else m.team_a_id
        opponent_name = m.team_b_name if is_team_a else m.team_a_name
        team_score = (m.score_a or 0) if is_team_a else (m.score_b or 0)
        opp_score = (m.score_b or 0) if is_team_a else (m.score_a or 0)

        result = "PENDING"
        if m.status == "COMPLETED":
            if m.winner_team_id == team_id:
                result = "WIN"
            elif m.winner_team_id:
                result = "LOSS"
            else:
                result = "DRAW"

        journey_steps.append({
            "match_id": m.id,
            "stage_id": m.stage_id,
            "stage_name": m.stage_name or m.stage,
            "round_id": m.round_id,
            "round_name": m.round_name or f"Round {m.round_number}",
            "match_number": m.match_number,
            "series_format": m.series_format,
            "opponent_id": opponent_id,
            "opponent_name": opponent_name,
            "score": f"{team_score} - {opp_score}",
            "result": result,
            "status": m.status,
            "scheduled_time": m.scheduled_time,
            "advancement": m.advancement if (m.advancement and m.advancement.get("advanced_team_id") == team_id) else None
        })

    return {
        "tournament_id": tournament.id,
        "tournament_name": getattr(tournament, "name", None) or getattr(tournament, "title", "Tournament"),
        "team_id": team_id,
        "team_name": team_name,
        "journey": journey_steps
    }
