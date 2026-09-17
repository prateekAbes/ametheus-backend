import secrets
import json
import math
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
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
    db: AsyncSession,
) -> Dict[str, Any]:
    # 1. Get registered teams
    res = await db.execute(
        select(Team)
        .join(TournamentRegistration, TournamentRegistration.team_id == Team.id)
        .where(TournamentRegistration.tournament_id == tournament.id)
    )
    teams = res.scalars().all()

    num_teams = len(teams)
    if num_teams < 2:
        num_teams = 4

    total_rounds = max(1, math.ceil(math.log2(num_teams)))
    matches_in_r1 = 2 ** (total_rounds - 1)

    bracket_tree = []
    bye_matches = []

    # Clear existing matches for this tournament if regenerating
    old_matches = await db.execute(select(Match).where(Match.tournament_id == tournament.id))
    for m in old_matches.scalars().all():
        await db.delete(m)
    await db.flush()

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
                # Seed teams into Round 1
                t_idx_a = (m_idx - 1) * 2
                t_idx_b = t_idx_a + 1
                if t_idx_a < len(teams):
                    team_a_id = teams[t_idx_a].id
                if t_idx_b < len(teams):
                    team_b_id = teams[t_idx_b].id

                if team_a_id and team_b_id:
                    status = "CHECK_IN_OPEN"
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
            db.add(db_match)
            await db.flush()

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

    tournament.bracket_json = json.dumps(bracket_tree)
    tournament.status = "LIVE"
    await db.commit()
    return {"total_rounds": total_rounds, "bracket": bracket_tree}

async def advance_bracket_match(
    tournament_id: str,
    completed_match: Match,
    winner_team_id: str,
    db: AsyncSession,
) -> Optional[Match]:
    """
    Advances the winner of completed_match into the next round match.
    """
    completed_match.winner_team_id = winner_team_id
    completed_match.status = "COMPLETED"
    await db.flush()

    # Find next round match
    next_round_num = completed_match.round_number + 1
    next_match_num = math.ceil(completed_match.match_number / 2)
    is_team_a = (completed_match.match_number % 2) != 0  # Odd match_number goes to team_a, even goes to team_b

    next_res = await db.execute(
        select(Match).where(
            Match.tournament_id == tournament_id,
            Match.round_number == next_round_num,
            Match.match_number == next_match_num,
        )
    )
    next_match = next_res.scalars().first()

    if next_match:
        if is_team_a:
            next_match.team_a_id = winner_team_id
        else:
            next_match.team_b_id = winner_team_id

        if next_match.team_a_id and next_match.team_b_id:
            next_match.status = "CHECK_IN_OPEN"

        await db.flush()
    else:
        # If no next match, this was the Grand Final! Tournament completed!
        t_res = await db.execute(select(Tournament).where(Tournament.id == tournament_id))
        tourn = t_res.scalars().first()
        if tourn:
            tourn.status = "COMPLETED"

    await db.commit()
    return next_match
