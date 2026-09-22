from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.models.game import Game
from app.schemas import GameResponse, GameCreate, GameUpdate
from app.services.audit import record_audit_log

router = APIRouter()

GAME_FALLBACK_IMAGES = {
    "valorant": {
        "banner": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
    },
    "counter-strike-2": {
        "banner": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop",
    },
    "bgmi": {
        "banner": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop",
    },
    "chess": {
        "banner": "https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1586165368502-1bad197a6461?q=80&w=300&auto=format&fit=crop",
    },
    "free-fire": {
        "banner": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1579783902614-a3fb3927b675?q=80&w=300&auto=format&fit=crop",
    },
}

def sanitize_game_images(slug: str, banner: str, logo: str):
    fb = GAME_FALLBACK_IMAGES.get(slug, {
        "banner": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
    })
    b = banner if (banner and "aida-public" not in banner and "_Banner" not in banner) else fb["banner"]
    l = logo if (logo and "aida-public" not in logo and "_Logo" not in logo) else fb["logo"]
    return b, l

def serialize_game_doc(g: dict, teams_count: int = 0) -> GameResponse:
    gid = str(g.get("_id", g.get("id")))
    b_url, l_url = sanitize_game_images(g.get("slug", ""), g.get("banner_url", ""), g.get("logo_url", ""))
    return GameResponse(
        id=gid,
        slug=g["slug"],
        name=g["name"],
        category=g.get("category", "pc"),
        platform=g["platform"],
        tag=g["tag"],
        is_team_game=g.get("is_team_game", True),
        min_team_size=g.get("min_team_size", 1),
        max_team_size=g.get("max_team_size", 5),
        max_substitutes=g.get("max_substitutes", 1),
        entry_fee_inr=g.get("entry_fee_inr", 0),
        prize_pool_inr=g.get("prize_pool_inr", 0),
        play_url=g.get("play_url"),
        banner_url=b_url,
        logo_url=l_url,
        description=g.get("description", ""),
        server_region=g.get("server_region", "AWS Mumbai (ap-south-1)"),
        anti_cheat=g.get("anti_cheat", "Standard Protocol"),
        match_protocol=g.get("match_protocol", "Custom Lobby"),
        accent_color=g.get("accent_color", "#E56A10"),
        status=g.get("status", "ACTIVE"),
        expected_release_date=g.get("expected_release_date"),
        announcement_text=g.get("announcement_text"),
        hero_media_url=g.get("hero_media_url"),
        hero_media_type=g.get("hero_media_type", "gif"),
        hero_media_enabled=g.get("hero_media_enabled", True),
        gallery=g.get("gallery", []),
        teams_count=teams_count,
    )

@router.get("", response_model=List[GameResponse])
async def list_games(
    status: Optional[str] = Query(None, description="Filter games by status e.g. ACTIVE, COMING_SOON"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"is_dummy": {"$ne": True}}
    if status:
        query["status"] = status.upper()
    cursor = db.games.find(query)
    games_docs = await cursor.to_list(length=100)
    
    response = []
    for g in games_docs:
        gid = str(g.get("_id", g.get("id")))
        t_count = await db.teams.count_documents({"$or": [{"game_id": gid}, {"game_slug": g.get("slug")}]})
        response.append(serialize_game_doc(g, t_count))
    return response

@router.get("/{slug}", response_model=GameResponse)
async def get_game_by_slug(slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    g = await db.games.find_one({"$or": [{"slug": slug}, {"_id": slug}, {"id": slug}]})
    if not g:
        raise HTTPException(status_code=404, detail="Game not found")
    
    gid = str(g.get("_id", g.get("id")))
    t_count = await db.teams.count_documents({"$or": [{"game_id": gid}, {"game_slug": g.get("slug")}]})
    return serialize_game_doc(g, t_count)

@router.post("", response_model=GameResponse, status_code=status.HTTP_201_CREATED)
async def create_game(
    payload: GameCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    existing = await db.games.find_one({"slug": payload.slug})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Game with slug '{payload.slug}' already exists",
        )

    game = Game(
        slug=payload.slug,
        name=payload.name,
        category=payload.category,
        platform=payload.platform,
        tag=payload.tag,
        is_team_game=payload.is_team_game,
        min_team_size=payload.min_team_size,
        max_team_size=payload.max_team_size,
        max_substitutes=payload.max_substitutes,
        entry_fee_inr=payload.entry_fee_inr,
        prize_pool_inr=payload.prize_pool_inr,
        play_url=payload.play_url,
        banner_url=payload.banner_url,
        logo_url=payload.logo_url,
        description=payload.description,
        server_region=payload.server_region,
        anti_cheat=payload.anti_cheat,
        match_protocol=payload.match_protocol,
        accent_color=payload.accent_color or "#E56A10",
        status=payload.status or "ACTIVE",
        expected_release_date=payload.expected_release_date,
        announcement_text=payload.announcement_text,
        hero_media_url=payload.hero_media_url,
        hero_media_type=payload.hero_media_type or "gif",
        hero_media_enabled=payload.hero_media_enabled if payload.hero_media_enabled is not None else True,
        gallery=payload.gallery or [],
    )
    doc = game.to_mongo()
    await db.games.insert_one(doc)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="CREATE_GAME",
        target=f"Game {game.name} ({game.slug})",
        details=f"Created game with entry fee ₹{game.entry_fee_inr}",
    )

    return serialize_game_doc(doc, 0)

@router.put("/{id}", response_model=GameResponse)
async def update_game(
    id: str,
    payload: GameUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    game = await db.games.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    update_data = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if update_data:
        await db.games.update_one({"_id": game["_id"]}, {"$set": update_data})

    updated_game = await db.games.find_one({"_id": game["_id"]})
    gid = str(updated_game.get("_id", updated_game.get("id")))
    t_count = await db.teams.count_documents({"$or": [{"game_id": gid}, {"game_slug": updated_game.get("slug")}]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPDATE_GAME",
        target=f"Game {updated_game.get('name')}",
        details=f"Updated game fields: {list(update_data.keys())}",
    )

    return serialize_game_doc(updated_game, t_count)

@router.delete("/{id}")
async def delete_game(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    game = await db.games.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    await db.games.delete_one({"_id": game["_id"]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="DELETE_GAME",
        target=f"Game {game.get('name')}",
        details="Removed game catalog entry",
    )

    return {"status": "success", "message": f"Game '{game.get('name')}' successfully deleted"}

@router.post("/{id}/publish", response_model=GameResponse)
async def publish_game(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    game = await db.games.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    await db.games.update_one(
        {"_id": game["_id"]},
        {"$set": {"status": "ACTIVE"}}
    )

    updated_game = await db.games.find_one({"_id": game["_id"]})
    gid = str(updated_game.get("_id", updated_game.get("id")))
    t_count = await db.teams.count_documents({"$or": [{"game_id": gid}, {"game_slug": updated_game.get("slug")}]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="PUBLISH_GAME",
        target=f"Game {updated_game.get('name')}",
        details="Transitioned status to ACTIVE",
    )

    return serialize_game_doc(updated_game, t_count)
