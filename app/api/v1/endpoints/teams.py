from typing import List, Union, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.team import Team, TeamMember
from app.models.base import get_utc_now
from app.services.audit import record_audit_log
from app.services.wallet_service import credit_wallet_funds
from app.services.notification import create_notification
from app.schemas import TeamCreate, TeamJoin, TeamResponse, TeamMemberResponse

router = APIRouter()

async def serialize_team(team: Union[dict, Team], db: AsyncIOMotorDatabase) -> TeamResponse:
    team_dict = team if isinstance(team, dict) else team.model_dump()
    team_id = str(team_dict.get("_id", team_dict.get("id")))

    game_id = team_dict.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": game_id}, {"id": game_id}, {"slug": game_id}]})

    captain_id = team_dict.get("captain_id")
    captain = await db.users.find_one({"$or": [{"_id": captain_id}, {"id": captain_id}]})

    raw_members = team_dict.get("members", [])
    raw_substitutes = team_dict.get("substitutes", [])
    user_ids = [m.get("user_id") if isinstance(m, dict) else m.user_id for m in (raw_members + raw_substitutes)]

    users_dict = {}
    if user_ids:
        cursor = db.users.find({"$or": [{"_id": {"$in": user_ids}}, {"id": {"$in": user_ids}}]})
        async for u in cursor:
            uid = str(u.get("_id", u.get("id")))
            users_dict[uid] = u.get("display_name") or u.get("username")

    def map_member(m, default_role="MEMBER"):
        mid = m.get("id", m.get("_id")) if isinstance(m, dict) else getattr(m, "id", None)
        muid = m.get("user_id") if isinstance(m, dict) else m.user_id
        mrole = m.get("role", default_role) if isinstance(m, dict) else m.role
        mhandle = m.get("game_handle", "") if isinstance(m, dict) else m.game_handle
        return TeamMemberResponse(
            id=str(mid),
            user_id=muid,
            username=users_dict.get(muid, "Gamer"),
            role=mrole,
            game_handle=mhandle,
        )

    cap_name = "Captain"
    if captain:
        cap_name = captain.get("display_name") or captain.get("username") or "Captain"

    return TeamResponse(
        id=team_id,
        game_slug=game["slug"] if game else "game",
        name=team_dict["name"],
        tag=team_dict["tag"],
        invite_code=team_dict["invite_code"],
        captain_id=captain_id,
        captain_username=cap_name,
        max_members=team_dict.get("max_members", 5),
        max_substitutes=team_dict.get("max_substitutes", 1),
        payment_status=team_dict.get("payment_status", "UNPAID"),
        payment_id=team_dict.get("payment_id"),
        members=[map_member(m, "MEMBER") for m in raw_members],
        substitutes=[map_member(m, "SUBSTITUTE") for m in raw_substitutes],
    )

@router.get("", response_model=List[TeamResponse])
async def list_teams(
    game_slug: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"is_dummy": {"$ne": True}}
    if game_slug:
        game = await db.games.find_one({"$or": [{"slug": game_slug}, {"_id": game_slug}, {"id": game_slug}]})
        if game:
            gid = str(game.get("_id", game.get("id")))
            query["game_id"] = gid
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"tag": {"$regex": search, "$options": "i"}},
        ]
    cursor = db.teams.find(query).skip(skip).limit(min(limit, 100))
    teams = await cursor.to_list(length=limit)
    return [await serialize_team(t, db) for t in teams]

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    game = await db.games.find_one({"$or": [{"slug": payload.game_slug}, {"_id": payload.game_slug}, {"id": payload.game_slug}]})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    if game.get("status") == "COMING_SOON":
        raise HTTPException(
            status_code=400,
            detail=f"{game.get('name', 'This game')} is coming soon and not yet active for team creation.",
        )

    is_team_game = game.get("is_team_game", True)
    max_size = game.get("max_team_size", 5)
    max_subs = game.get("max_substitutes", 1)

    if not is_team_game or max_size <= 1:
        raise HTTPException(
            status_code=400,
            detail=f"{game.get('name', 'This game')} is a solo title and does not support team creation.",
        )

    gid = str(game.get("_id", game.get("id")))

    # Enforce one-team-per-game rule (Requirement 19)
    existing_team = await db.teams.find_one({
        "game_id": gid,
        "is_dummy": {"$ne": True},
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
    })
    if existing_team:
        raise HTTPException(
            status_code=400,
            detail=f"You already belong to team '{existing_team.get('name')}' for {game.get('name', 'this game')}. Each contender can only lead or join one team per game discipline.",
        )

    captain_member = TeamMember(
        user_id=current_user.id,
        role="CAPTAIN",
        game_handle=payload.captain_handle,
    )

    initial_payment_status = "UNPAID" if game.get("entry_fee_inr", 0) > 0 else "PAID"
    team = Team(
        game_id=gid,
        captain_id=current_user.id,
        name=payload.name,
        tag=payload.tag.upper(),
        max_members=max_size,
        max_substitutes=max_subs,
        members=[captain_member],
        substitutes=[],
        payment_status=initial_payment_status,
    )
    team_doc = team.to_mongo()
    await db.teams.insert_one(team_doc)

    return await serialize_team(team_doc, db)

@router.post("/join", response_model=TeamResponse)
async def join_team(
    payload: TeamJoin,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    clean_code = payload.invite_code.strip().upper()
    team = await db.teams.find_one({"invite_code": clean_code})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No team found.")

    game_id = team.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": game_id}, {"id": game_id}, {"slug": game_id}]})
    if game and game.get("status") == "COMING_SOON":
        raise HTTPException(
            status_code=400,
            detail=f"{game.get('name', 'This game')} is coming soon and not yet active.",
        )
    if game and (not game.get("is_team_game", True) or game.get("max_team_size", 5) <= 1):
        raise HTTPException(
            status_code=400,
            detail="Cannot join a team for a solo discipline.",
        )

    # Enforce one-team-per-game rule (Requirement 19)
    existing_team = await db.teams.find_one({
        "game_id": game_id,
        "is_dummy": {"$ne": True},
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
    })
    if existing_team:
        raise HTTPException(
            status_code=400,
            detail=f"You already belong to team '{existing_team.get('name')}' for this game discipline. Each contender can only lead or join one team per game.",
        )

    members = team.get("members", [])
    substitutes = team.get("substitutes", [])

    all_user_ids = [
        (m.get("user_id") if isinstance(m, dict) else m.user_id) for m in (members + substitutes)
    ]
    if current_user.id in all_user_ids:
        raise HTTPException(status_code=400, detail="User is already on this team roster.")

    team_id = str(team.get("_id", team.get("id")))

    if payload.is_substitute:
        max_subs = team.get("max_substitutes", 1)
        if len(substitutes) >= max_subs:
            raise HTTPException(status_code=400, detail="Team substitutes roster is already full.")
        new_sub = TeamMember(
            team_id=team_id,
            user_id=current_user.id,
            role="SUBSTITUTE",
            game_handle=payload.player_handle,
        )
        await db.teams.update_one(
            {"_id": team["_id"]},
            {"$push": {"substitutes": new_sub.model_dump()}}
        )
    else:
        max_cap = team.get("max_members") or (game.get("max_team_size", 5) if game else 5)
        if len(members) >= max_cap:
            raise HTTPException(status_code=400, detail="Team main roster is already full.")
        new_member = TeamMember(
            team_id=team_id,
            user_id=current_user.id,
            role="MEMBER",
            game_handle=payload.player_handle,
        )
        await db.teams.update_one(
            {"_id": team["_id"]},
            {"$push": {"members": new_member.model_dump()}}
        )

    updated_team = await db.teams.find_one({"_id": team["_id"]})
    return await serialize_team(updated_team, db)

@router.get("/my-teams", response_model=List[TeamResponse])
async def get_my_teams(
    game_slug: str = None,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
        "is_dummy": {"$ne": True},
    }
    if game_slug:
        game = await db.games.find_one({"slug": game_slug})
        if game:
            gid = str(game.get("_id", game.get("id")))
            query["game_id"] = gid

    cursor = db.teams.find(query)
    teams = await cursor.to_list(length=100)
    return [await serialize_team(t, db) for t in teams]

@router.get("/code/{code}", response_model=TeamResponse)
async def get_team_by_code(code: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    clean_code = code.strip().upper()
    team = await db.teams.find_one({"invite_code": clean_code})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No team found.")
    return await serialize_team(team, db)

@router.get("/{id}", response_model=TeamResponse)
async def get_team(id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return await serialize_team(team, db)

@router.post("/{id}/leave")
async def leave_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The captain cannot leave the team. Disband the team or transfer leadership.",
        )

    await db.teams.update_one(
        {"_id": team["_id"]},
        {
            "$pull": {
                "members": {"user_id": current_user.id},
                "substitutes": {"user_id": current_user.id},
            }
        }
    )
    return {"status": "success", "message": f"Successfully left team {team['name']}"}

@router.post("/{id}/kick/{user_id}")
async def kick_team_member(
    id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can remove roster members.",
        )

    if user_id == team.get("captain_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot kick the team captain.",
        )

    await db.teams.update_one(
        {"_id": team["_id"]},
        {
            "$pull": {
                "members": {"user_id": user_id},
                "substitutes": {"user_id": user_id},
            }
        }
    )
    return {"status": "success", "message": "Member removed from team roster"}

@router.delete("/{id}")
async def delete_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can disband the team.",
        )

    team_id = str(team.get("_id", team.get("id")))
    team_name = team.get("name", "Team")
    team_ids_query = [team_id]
    if team.get("id") and str(team.get("id")) != team_id:
        team_ids_query.append(str(team.get("id")))
    if team.get("_id") and str(team.get("_id")) != team_id:
        team_ids_query.append(str(team.get("_id")))

    now_utc = datetime.now(timezone.utc)

    # 1. Collect all roster members for notifications
    members = team.get("members", []) or []
    substitutes = team.get("substitutes", []) or []
    captain_id = team.get("captain_id")
    all_roster_user_ids = set()
    if captain_id:
        all_roster_user_ids.add(captain_id)
    for m in members + substitutes:
        uid = m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)
        if uid:
            all_roster_user_ids.add(uid)

    # 2. Find all active/pending registrations for this team
    registrations = await db.tournament_registrations.find({
        "team_id": {"$in": team_ids_query},
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    }).to_list(length=200)

    reg_ids = [str(r.get("_id", r.get("id"))) for r in registrations if r.get("_id") or r.get("id")]

    # 3. Track refunded payments to ensure strict idempotency
    refunded_payment_count = 0
    total_refunded_paise = 0
    refunded_payment_ids = set()

    # 3A. Refund successful payments in db.game_payments (UPI or direct payment records)
    payment_query = {
        "status": "APPROVED",
        "is_refunded": {"$ne": True},
        "$or": [
            {"team_id": {"$in": team_ids_query}},
        ]
    }
    if reg_ids:
        payment_query["$or"].append({"registration_id": {"$in": reg_ids}})

    approved_payments = await db.game_payments.find(payment_query).to_list(length=200)

    for p in approved_payments:
        p_id = str(p.get("_id", p.get("id")))
        if p_id in refunded_payment_ids:
            continue

        payer_id = p.get("user_id")
        amount_paise = int(p.get("amount_paise", 0))
        ref_id = f"refund_pay_{p_id}"

        if amount_paise > 0 and payer_id:
            # Atomic idempotency guard in wallet ledger
            existing_refund = await db.wallet_ledger.find_one({
                "user_id": payer_id,
                "reference_id": ref_id,
                "txn_type": "TEAM_DISBAND_REFUND",
            })
            if not existing_refund:
                await credit_wallet_funds(
                    db=db,
                    user_id=payer_id,
                    amount_paise=amount_paise,
                    txn_type="TEAM_DISBAND_REFUND",
                    reference_id=ref_id,
                    description=f"Refund: Team '{team_name}' disbanded (Payment {p.get('utr') or p_id[:8]})",
                    admin_id=current_user.id if current_user.role == "SUPER_ADMIN" else None,
                )
                refunded_payment_count += 1
                total_refunded_paise += amount_paise
            refunded_payment_ids.add(p_id)

            # Mark payment record as refunded while keeping original details intact
            await db.game_payments.update_one(
                {"_id": p["_id"]},
                {
                    "$set": {
                        "is_refunded": True,
                        "refunded_at": now_utc,
                        "refund_amount_paise": amount_paise,
                        "refund_destination": "PLAYER_WALLET",
                        "refund_reason": f"Team '{team_name}' disbanded by {current_user.username}",
                    }
                },
            )

    # 3B. Refund registrations paid via WALLET directly
    for reg in registrations:
        if reg.get("payment_method") == "WALLET" and reg.get("payment_status") == "PAID" and not reg.get("is_refunded"):
            reg_id = str(reg.get("_id", reg.get("id")))
            ref_id = f"refund_reg_{reg_id}"

            existing_refund = await db.wallet_ledger.find_one({
                "reference_id": ref_id,
                "txn_type": "TEAM_DISBAND_REFUND",
            })
            amount_paise = 0
            payer_id = None
            if not existing_refund:
                # Identify payer and original debit amount from wallet ledger
                debit_ledger = await db.wallet_ledger.find_one({
                    "$or": [
                        {"reference_id": reg_id},
                        {"reference_id": reg.get("order_id")},
                        {"reference_id": reg.get("wallet_txn_id")}
                    ],
                    "txn_type": "TOURNAMENT_ENTRY",
                })
                if debit_ledger:
                    payer_id = debit_ledger.get("user_id")
                    amount_paise = abs(int(debit_ledger.get("amount_paise", 0)))
                else:
                    payer_id = reg.get("player_id") or team.get("captain_id")
                    tourn = await db.tournaments.find_one({"$or": [{"_id": reg.get("tournament_id")}, {"id": reg.get("tournament_id")}, {"slug": reg.get("tournament_id")}]})
                    amount_paise = int(round((tourn.get("entry_fee_inr", 0) if tourn else 0) * 100))

                if amount_paise > 0 and payer_id:
                    await credit_wallet_funds(
                        db=db,
                        user_id=payer_id,
                        amount_paise=amount_paise,
                        txn_type="TEAM_DISBAND_REFUND",
                        reference_id=ref_id,
                        description=f"Refund: Team '{team_name}' disbanded (Registration {reg_id[:8]})",
                        admin_id=current_user.id if current_user.role == "SUPER_ADMIN" else None,
                    )
                    refunded_payment_count += 1
                    total_refunded_paise += amount_paise

            await db.tournament_registrations.update_one(
                {"_id": reg["_id"]},
                {
                    "$set": {
                        "is_refunded": True,
                        "refunded_at": now_utc,
                        "refund_amount_paise": amount_paise,
                    }
                },
            )

    # 4. Invalidate and cancel all tournament registrations for this team
    await db.tournament_registrations.update_many(
        {"team_id": {"$in": team_ids_query}},
        {
            "$set": {
                "status": "CANCELLED",
                "payment_status": "CANCELLED",
                "cancelled_at": now_utc,
                "cancellation_reason": f"Team '{team_name}' disbanded",
            }
        },
    )

    # 5. Clean up any scheduled match slots holding this team
    await db.matches.update_many(
        {"team_a_id": {"$in": team_ids_query}, "status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]}},
        {"$set": {"team_a_id": None, "team_a_name": "TBD (Slot 1)", "team_a_tag": "TBD"}}
    )
    await db.matches.update_many(
        {"team_b_id": {"$in": team_ids_query}, "status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]}},
        {"$set": {"team_b_id": None, "team_b_name": "TBD (Slot 2)", "team_b_tag": "TBD"}}
    )

    # 6. Notify roster members of the disbandment and wallet refunds
    for uid in all_roster_user_ids:
        await create_notification(
            db=db,
            user_id=uid,
            title="Team Disbanded",
            message=f"Team '{team_name}' has been disbanded. Any tournament registrations have been cancelled and eligible entry fees refunded to your Ametheus wallet.",
            notification_type="TEAM",
            link="/dashboard",
        )

    # 7. Record system audit log
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="DISBAND_TEAM",
        target=f"Team {team_name} ({team_id})",
        details=f"Disbanded team. Cancelled {len(registrations)} registration(s). Refunded {refunded_payment_count} payment(s) totaling ₹{total_refunded_paise / 100:.2f} to player wallets.",
    )

    # 8. Delete the team record from the database
    await db.teams.delete_many({"$or": [{"_id": team["_id"]}, {"id": team_id}]})

    return {
        "status": "success",
        "message": f"Team '{team_name}' has been disbanded. Active registrations were cancelled and eligible entry fees refunded to player wallets.",
        "refunded_payments_count": refunded_payment_count,
        "total_refunded_inr": total_refunded_paise / 100,
    }
