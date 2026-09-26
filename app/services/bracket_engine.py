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

    for attr in ["match_format", "series_format", "format"]:
        default_fmt = getattr(tournament, attr, None)
        if default_fmt in ["Best of 1", "Best of 3", "Best of 5"]:
            return default_fmt
        if default_fmt in ["BO1", "BO3", "BO5"]:
            return default_fmt
    return "BO3" if r == total_rounds else "BO1"

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
        format="BO5",
        series_format="BO5",
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
    Advances the winner of completed_match into the next round match.
    Matches remain SCHEDULED until admin triggers MARK AS LIVE.
    """
    completed_match.winner_team_id = winner_team_id
    completed_match.status = "COMPLETED"
    await db.matches.update_one(
        {"$or": [{"_id": completed_match.id}, {"id": completed_match.id}]},
        {"$set": {"winner_team_id": winner_team_id, "status": "COMPLETED"}}
    )

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

        # Retain status as SCHEDULED (Rule 15: match only becomes LIVE when admin clicks MARK AS LIVE)
        if next_match.status != "LIVE":
            update_data["status"] = "SCHEDULED"

        await db.matches.update_one({"_id": next_match_doc["_id"]}, {"$set": update_data})
    else:
        # Check if all tournament matches are completed
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
