import re
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, Query, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_super_admin, require_organizer
from app.models.user import User
from app.schemas import LeaderboardEntryResponse

router = APIRouter()

class LeaderboardStatsSchema(BaseModel):
    total_awarded_prizes: str = "₹0"
    active_contenders: str = "0"
    verified_squads: str = "0"
    top_team_earnings: str = "₹0"
    top_team_name: str = ""

class LeaderboardStatsUpdate(BaseModel):
    total_awarded_prizes: Optional[str] = None
    active_contenders: Optional[str] = None
    verified_squads: Optional[str] = None
    top_team_earnings: Optional[str] = None
    top_team_name: Optional[str] = None

class CustomLeaderboardEntryCreate(BaseModel):
    name: str
    tag: str
    type: str = "team"
    game_slug: str = "valorant"
    game_name: str = "Valorant"
    captain: Optional[str] = "@captain"
    logo_url: Optional[str] = None
    win_rate: float = 80.0
    matches_played: int = 10
    wins: int = 8
    losses: int = 2
    rating: int = 2000
    earnings: int = 25000
    category: str = "pc"

class LeaderboardCategorySchema(BaseModel):
    id: str
    name: str
    slug: str
    description: Optional[str] = ""
    game_slug: str = "all"
    sort_metric: str = "rating"
    min_matches: int = 0
    is_system: bool = False
    created_at: Optional[str] = None

class LeaderboardCategoryCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = ""
    game_slug: Optional[str] = "all"
    sort_metric: Optional[str] = "rating"
    min_matches: Optional[int] = 0

DEFAULT_CATEGORIES = [
    {
        "_id": "cat_overall",
        "name": "Overall Standings",
        "slug": "overall",
        "description": "Ranked by platform Elo rating across all competitive titles",
        "game_slug": "all",
        "sort_metric": "rating",
        "min_matches": 0,
        "is_system": True,
    },
    {
        "_id": "cat_win_rate",
        "name": "Highest Win Rate",
        "slug": "win_rate",
        "description": "Teams with highest match win percentage (min 3 matches)",
        "game_slug": "all",
        "sort_metric": "win_rate",
        "min_matches": 3,
        "is_system": True,
    },
    {
        "_id": "cat_earnings",
        "name": "Top Prize Winners",
        "slug": "earnings",
        "description": "Ranked by total verified cash tournament earnings",
        "game_slug": "all",
        "sort_metric": "earnings",
        "min_matches": 0,
        "is_system": True,
    },
    {
        "_id": "cat_wins",
        "name": "Most Match Wins",
        "slug": "wins",
        "description": "Ranked by total match victories in sanctioned events",
        "game_slug": "all",
        "sort_metric": "wins",
        "min_matches": 0,
        "is_system": True,
    },
    {
        "_id": "cat_pc",
        "name": "PC Competitions",
        "slug": "pc",
        "description": "Leaderboards for PC gaming titles (Valorant, CS2)",
        "game_slug": "all",
        "sort_metric": "rating",
        "min_matches": 0,
        "is_system": True,
    },
    {
        "_id": "cat_mobile",
        "name": "Mobile Competitions",
        "slug": "mobile",
        "description": "Leaderboards for Mobile titles (BGMI, Free Fire)",
        "game_slug": "all",
        "sort_metric": "rating",
        "min_matches": 0,
        "is_system": True,
    },
]



async def ensure_default_categories(db: AsyncIOMotorDatabase):
    count = await db.leaderboard_categories.count_documents({})
    if count == 0:
        for c in DEFAULT_CATEGORIES:
            doc = dict(c)
            doc["created_at"] = datetime.now(timezone.utc)
            await db.leaderboard_categories.insert_one(doc)

@router.get("/stats", response_model=LeaderboardStatsSchema)
async def get_leaderboard_stats(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Retrieve platform overview statistics for leaderboard header (calculated dynamically from real DB data)."""
    doc = await db.platform_settings.find_one({"_id": "leaderboard_stats"}) or {}

    # Real counts from database
    real_contenders = await db.users.count_documents({"is_dummy": {"$ne": True}})
    real_squads = await db.teams.count_documents({"is_dummy": {"$ne": True}})

    # Real total awarded prizes from completed payouts
    paid_payouts = await db.payouts.find({"status": "PAID", "is_dummy": {"$ne": True}}).to_list(500)
    total_prizes = sum(int(p.get("amount_paise", 0)) // 100 for p in paid_payouts)

    total_awarded_str = doc.get("total_awarded_prizes") or (f"₹{total_prizes:,}" if total_prizes > 0 else "₹0")
    active_contenders_str = doc.get("active_contenders") or str(real_contenders)
    verified_squads_str = doc.get("verified_squads") or str(real_squads)
    top_team_earnings_str = doc.get("top_team_earnings") or "₹0"
    top_team_name_str = doc.get("top_team_name") or ""

    return LeaderboardStatsSchema(
        total_awarded_prizes=total_awarded_str,
        active_contenders=active_contenders_str,
        verified_squads=verified_squads_str,
        top_team_earnings=top_team_earnings_str,
        top_team_name=top_team_name_str,
    )

@router.patch("/stats", response_model=LeaderboardStatsSchema)
async def update_leaderboard_stats(
    payload: LeaderboardStatsUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to update the 4 leaderboard overview cards."""
    updates = {}
    if payload.total_awarded_prizes is not None:
        updates["total_awarded_prizes"] = payload.total_awarded_prizes
    if payload.active_contenders is not None:
        updates["active_contenders"] = payload.active_contenders
    if payload.verified_squads is not None:
        updates["verified_squads"] = payload.verified_squads
    if payload.top_team_earnings is not None:
        updates["top_team_earnings"] = payload.top_team_earnings
    if payload.top_team_name is not None:
        updates["top_team_name"] = payload.top_team_name

    if updates:
        await db.platform_settings.update_one(
            {"_id": "leaderboard_stats"},
            {"$set": updates},
            upsert=True,
        )
    current = await db.platform_settings.find_one({"_id": "leaderboard_stats"}) or {}
    return LeaderboardStatsSchema(
        total_awarded_prizes=current.get("total_awarded_prizes", "₹0"),
        active_contenders=current.get("active_contenders", "0"),
        verified_squads=current.get("verified_squads", "0"),
        top_team_earnings=current.get("top_team_earnings", "₹0"),
        top_team_name=current.get("top_team_name", ""),
    )

@router.post("/entries", response_model=LeaderboardEntryResponse)
async def create_leaderboard_entry(
    payload: CustomLeaderboardEntryCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to add a contender or team directly onto the leaderboard."""
    entry_id = f"custom_lb_{uuid.uuid4().hex[:8]}"
    doc = {
        "_id": entry_id,
        "id": entry_id,
        "name": payload.name,
        "tag": payload.tag.upper(),
        "type": payload.type,
        "game_slug": payload.game_slug,
        "game_name": payload.game_name,
        "captain": payload.captain or f"@{payload.name.lower()}",
        "logo_url": payload.logo_url or "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=300",
        "win_rate": payload.win_rate,
        "matches_played": payload.matches_played,
        "wins": payload.wins,
        "losses": payload.losses,
        "rating": payload.rating,
        "earnings": payload.earnings,
        "form": ["W", "W", "W", "W", "W"],
        "is_verified": True,
        "category": payload.category,
        "created_at": datetime.now(timezone.utc),
    }
    await db.custom_leaderboard_entries.insert_one(doc)
    doc["rank"] = 1
    return LeaderboardEntryResponse(**doc)

@router.get("/categories", response_model=List[LeaderboardCategorySchema])
async def list_leaderboard_categories(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Retrieve all available leaderboard ranking categories."""
    await ensure_default_categories(db)
    cursor = db.leaderboard_categories.find().sort("created_at", 1)
    cats = await cursor.to_list(length=100)
    return [
        LeaderboardCategorySchema(
            id=str(c.get("_id", c.get("id"))),
            name=c["name"],
            slug=c["slug"],
            description=c.get("description", ""),
            game_slug=c.get("game_slug", "all"),
            sort_metric=c.get("sort_metric", "rating"),
            min_matches=c.get("min_matches", 0),
            is_system=c.get("is_system", False),
            created_at=str(c.get("created_at")),
        )
        for c in cats
    ]

@router.post("/categories", response_model=LeaderboardCategorySchema)
async def create_leaderboard_category(
    payload: LeaderboardCategoryCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to create a new leaderboard ranking category with automated calculations."""
    raw_slug = payload.slug or payload.name
    slug = re.sub(r"[^\w\s-]", "", raw_slug.lower().strip())
    slug = re.sub(r"[\s_-]+", "_", slug)

    existing = await db.leaderboard_categories.find_one({"slug": slug})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Category with slug '{slug}' already exists.",
        )

    valid_metrics = ["rating", "win_rate", "earnings", "wins", "matches_played"]
    metric = payload.sort_metric if payload.sort_metric in valid_metrics else "rating"

    new_cat = {
        "_id": f"cat_{uuid.uuid4().hex[:12]}",
        "name": payload.name,
        "slug": slug,
        "description": payload.description or f"Ranked by {metric} for {payload.game_slug}",
        "game_slug": payload.game_slug or "all",
        "sort_metric": metric,
        "min_matches": payload.min_matches or 0,
        "is_system": False,
        "created_at": datetime.now(timezone.utc),
    }
    await db.leaderboard_categories.insert_one(new_cat)

    return LeaderboardCategorySchema(
        id=str(new_cat["_id"]),
        name=new_cat["name"],
        slug=new_cat["slug"],
        description=new_cat["description"],
        game_slug=new_cat["game_slug"],
        sort_metric=new_cat["sort_metric"],
        min_matches=new_cat["min_matches"],
        is_system=new_cat["is_system"],
        created_at=str(new_cat["created_at"]),
    )

@router.delete("/categories/{slug}")
async def delete_leaderboard_category(
    slug: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to delete a custom leaderboard category."""
    cat = await db.leaderboard_categories.find_one({"slug": slug})
    if not cat:
        raise HTTPException(status_code=404, detail="Leaderboard category not found")
    if cat.get("is_system"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete default platform system categories.",
        )
    await db.leaderboard_categories.delete_one({"_id": cat["_id"]})
    return {"status": "success", "message": f"Category '{slug}' deleted successfully"}

class LeaderboardDisqualificationCreate(BaseModel):
    entity_id: str
    entity_name: str
    entity_tag: Optional[str] = ""
    reason: str = "Administrative Removal"
    notes: Optional[str] = ""

@router.get("/disqualifications")
async def list_disqualifications(
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all entities currently disqualified from leaderboard standings."""
    docs = await db.leaderboard_disqualifications.find().sort("disqualified_at", -1).to_list(100)
    for d in docs:
        d["id"] = str(d.pop("_id", d.get("id")))
        if isinstance(d.get("disqualified_at"), datetime):
            d["disqualified_at"] = d["disqualified_at"].isoformat()
    return docs

@router.post("/disqualify")
async def disqualify_leaderboard_entry(
    payload: LeaderboardDisqualificationCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to remove/disqualify a player or team from leaderboard rankings."""
    existing = await db.leaderboard_disqualifications.find_one({"entity_id": payload.entity_id})
    if existing:
        return {"status": "exists", "message": f"{payload.entity_name} is already disqualified"}
    
    doc = {
        "_id": f"dq_{uuid.uuid4().hex[:12]}",
        "entity_id": payload.entity_id,
        "entity_name": payload.entity_name,
        "entity_tag": payload.entity_tag or "",
        "reason": payload.reason,
        "notes": payload.notes or "",
        "disqualified_by": admin_user.username,
        "disqualified_at": datetime.now(timezone.utc),
    }
    await db.leaderboard_disqualifications.insert_one(doc)
    from app.services.audit import record_audit_log
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="LEADERBOARD_DISQUALIFY",
        target=f"{payload.entity_name} ({payload.entity_id})",
        details=f"Reason: {payload.reason}. Notes: {payload.notes}",
    )
    return {"status": "success", "message": f"{payload.entity_name} removed and disqualified from leaderboard"}

@router.delete("/disqualify/{entity_id}")
async def reinstate_leaderboard_entry(
    entity_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Reinstate a previously disqualified entity back into leaderboard standings."""
    res = await db.leaderboard_disqualifications.delete_one({"entity_id": entity_id})
    if res.deleted_count == 0:
        await db.leaderboard_disqualifications.delete_one({"_id": entity_id})
    from app.services.audit import record_audit_log
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="LEADERBOARD_REINSTATE",
        target=f"Entity {entity_id}",
    )
    return {"status": "success", "message": f"Entity {entity_id} reinstated into standings"}

@router.delete("/entries/{entity_id}")
async def remove_entry_direct(
    entity_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Direct deletion/disqualification shortcut for an entry."""
    await db.custom_leaderboard_entries.delete_one({"$or": [{"_id": entity_id}, {"id": entity_id}]})
    doc = {
        "_id": f"dq_{uuid.uuid4().hex[:12]}",
        "entity_id": entity_id,
        "entity_name": entity_id,
        "entity_tag": "",
        "reason": "Administrative Removal",
        "notes": "Removed by administrator via direct action",
        "disqualified_by": admin_user.username,
        "disqualified_at": datetime.now(timezone.utc),
    }
    await db.leaderboard_disqualifications.insert_one(doc)
    return {"status": "success", "message": f"Entry {entity_id} removed from leaderboard"}

@router.get("", response_model=List[LeaderboardEntryResponse])
async def get_leaderboard(
    category: Optional[str] = Query(None),
    game: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Dynamically calculate leaderboard standings based on category metrics and game filters.
    """
    entries: List[dict] = []

    # Check category definition
    cat_doc = None
    if category and category != "all":
        cat_doc = await db.leaderboard_categories.find_one({"slug": category})

    sort_metric = cat_doc.get("sort_metric", "rating") if cat_doc else "rating"
    target_game = cat_doc.get("game_slug", "all") if cat_doc else "all"
    min_matches = cat_doc.get("min_matches", 0) if cat_doc else 0

    # Explicit game param takes precedence
    if game and game != "all":
        target_game = game

    try:
        cursor = db.teams.find({"is_dummy": {"$ne": True}})
        db_teams = await cursor.to_list(length=100)

        for idx, team in enumerate(db_teams):
            team_id = str(team.get("_id", team.get("id")))
            team_ids = [team_id]
            if "id" in team and str(team["id"]) not in team_ids:
                team_ids.append(str(team["id"]))
            if "_id" in team and str(team["_id"]) not in team_ids:
                team_ids.append(str(team["_id"]))

            gid = team.get("game_id")
            game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]}) if gid else None
            game_slug = game_obj["slug"] if game_obj else None
            game_name = game_obj["name"] if game_obj else None
            game_cat = game_obj.get("category", "pc") if game_obj else "pc"

            cap_id = team.get("captain_id")
            captain = await db.users.find_one({"$or": [{"_id": cap_id}, {"id": cap_id}]}) if cap_id else None
            captain_user = f"@{captain['username']}" if captain and captain.get("username") else "@captain"

            # Query completed matches for this team across both team_a and team_b positions
            completed_cursor = db.matches.find({
                "$and": [
                    {"$or": [
                        {"team_a_id": {"$in": team_ids}},
                        {"team_b_id": {"$in": team_ids}},
                    ]},
                    {"status": "COMPLETED"},
                    {"is_dummy": {"$ne": True}},
                ]
            })
            completed_matches = await completed_cursor.to_list(length=100)

            matches_played = len(completed_matches)
            wins = sum(1 for m in completed_matches if m.get("winner_team_id") in team_ids)
            losses = matches_played - wins
            # Real win rate — 0.0 if no matches played
            win_rate = round((wins / matches_played * 100), 1) if matches_played > 0 else 0.0

            # Compute recent form from last 5 completed matches
            recent = sorted(completed_matches, key=lambda m: str(m.get("completed_at", m.get("updated_at", ""))), reverse=True)[:5]
            form = ["W" if m.get("winner_team_id") in team_ids else "L" for m in recent]

            # Query real earnings from verified payouts
            payout_docs = await db.payouts.find({
                "$or": [
                    {"user_id": cap_id},
                    {"team_id": {"$in": team_ids}},
                ],
                "status": "PAID",
                "is_dummy": {"$ne": True},
            }).to_list(length=100)
            real_earnings = sum(int(p.get("amount_paise", 0)) // 100 for p in payout_docs)

            entries.append({
                "rank": idx + 1,
                "id": team_id,
                "name": team.get("name", "Team"),
                "tag": team.get("tag", "TM"),
                "type": "team",
                "game_slug": game_slug,
                "game_name": game_name,
                "captain": captain_user,
                "logo_url": team.get("logo_url") or None,
                "win_rate": win_rate,
                "matches_played": matches_played,
                "wins": wins,
                "losses": losses,
                "rating": max(1000, 1500 + (wins * 25) - (losses * 10)),
                "earnings": real_earnings,
                "form": form if form else ["-"],
                "is_verified": True,
                "category": game_cat,
            })

        # Also query custom leaderboard entries (admin-created entries only)
        custom_cursor = db.custom_leaderboard_entries.find({"is_dummy": {"$ne": True}})
        custom_docs = await custom_cursor.to_list(length=100)
        for c in custom_docs:
            c_id = str(c.get("_id", c.get("id")))
            entries.append({
                "rank": 1,
                "id": c_id,
                "name": c.get("name", "Team"),
                "tag": c.get("tag", "TM"),
                "type": c.get("type", "team"),
                "game_slug": c.get("game_slug"),
                "game_name": c.get("game_name"),
                "captain": c.get("captain", "@captain"),
                "logo_url": c.get("logo_url"),
                "win_rate": float(c.get("win_rate", 0.0)),
                "matches_played": int(c.get("matches_played", 0)),
                "wins": int(c.get("wins", 0)),
                "losses": int(c.get("losses", 0)),
                "rating": int(c.get("rating", 1500)),
                "earnings": int(c.get("earnings", 0)),
                "form": c.get("form", []),
                "is_verified": bool(c.get("is_verified", False)),
                "category": c.get("category", "pc"),
            })
    except Exception as e:
        print(f"[Leaderboard DB Query Notice]: {e}")

    # NOTE: No mock/fallback data augmentation. Empty DB = empty leaderboard.

    # Apply game filter if specified
    if target_game and target_game != "all":
        entries = [e for e in entries if e["game_slug"] == target_game]

    # Apply platform category filter (pc / mobile) if category matches
    if category in ["pc", "mobile"]:
        entries = [e for e in entries if e["category"] == category]

    # Apply minimum matches filter
    if min_matches > 0:
        entries = [e for e in entries if e["matches_played"] >= min_matches]

    # Fetch disqualified entity IDs and tags from db
    try:
        dq_docs = await db.leaderboard_disqualifications.find().to_list(length=500)
        dq_ids = {str(d.get("entity_id", "")) for d in dq_docs}
        dq_names = {str(d.get("entity_name", "")).lower() for d in dq_docs if d.get("entity_name")}
        dq_tags = {str(d.get("entity_tag", "")).lower() for d in dq_docs if d.get("entity_tag")}

        entries = [
            e for e in entries
            if str(e.get("id", "")) not in dq_ids
            and str(e.get("name", "")).lower() not in dq_names
            and (not e.get("tag") or str(e.get("tag", "")).lower() not in dq_tags)
        ]
    except Exception as e:
        print(f"[Leaderboard Disqualification Filter Notice]: {e}")

    # Dynamic metric sorting
    if sort_metric == "win_rate":
        entries.sort(key=lambda x: (x["win_rate"], x["wins"]), reverse=True)
    elif sort_metric == "earnings":
        entries.sort(key=lambda x: (x["earnings"], x["wins"]), reverse=True)
    elif sort_metric == "wins":
        entries.sort(key=lambda x: (x["wins"], x["win_rate"]), reverse=True)
    elif sort_metric == "matches_played":
        entries.sort(key=lambda x: (x["matches_played"], x["wins"]), reverse=True)
    else:  # rating
        entries.sort(key=lambda x: x["rating"], reverse=True)

    for rank, entry in enumerate(entries, start=1):
        entry["rank"] = rank

    return [LeaderboardEntryResponse(**e) for e in entries]
