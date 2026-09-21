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
    "call-of-duty-mobile": {
        "banner": "https://images.unsplash.com/photo-1542751110-97427bbecf20?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?q=80&w=300&auto=format&fit=crop",
    },
    "free-fire": {
        "banner": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1579783902614-a3fb3927b675?q=80&w=300&auto=format&fit=crop",
    },
    "rocket-league": {
        "banner": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
    },
    "smash-karts": {
        "banner": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop",
    },
    "mini-militia": {
        "banner": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop",
        "logo": "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop",
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

def serialize_game_doc(g: dict, count: int = 0) -> GameResponse:
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
        active_tournaments_count=count,
    )

@router.get("", response_model=List[GameResponse])
async def list_games(
    status: Optional[str] = Query(None, description="Filter games by status e.g. ACTIVE, COMING_SOON"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {}
    if status:
        query["status"] = status.upper()
    cursor = db.games.find(query)
    games_docs = await cursor.to_list(length=100)
    
    response = []
    for g in games_docs:
        gid = str(g.get("_id", g.get("id")))
        count = await db.tournaments.count_documents({"$or": [{"game_id": gid}, {"game_slug": g.get("slug")}]})
        response.append(serialize_game_doc(g, count))
    return response

@router.get("/{slug}", response_model=GameResponse)
async def get_game_by_slug(slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    g = await db.games.find_one({"$or": [{"slug": slug}, {"_id": slug}, {"id": slug}]})
    if not g:
        raise HTTPException(status_code=404, detail="Game not found")
    
    gid = str(g.get("_id", g.get("id")))
    count = await db.tournaments.count_documents({"$or": [{"game_id": gid}, {"game_slug": g.get("slug")}]})
    return serialize_game_doc(g, count)

@router.post("", response_model=GameResponse, status_code=status.HTTP_201_CREATED)
async def create_game(
    payload: GameCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin privilege: Register a new sanctioned competitive title in MongoDB.
    """
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
        details=f"Added new competitive game title {game.name}",
    )

    return serialize_game_doc(doc, 0)

@router.put("/{id}", response_model=GameResponse)
async def update_game(
    id: str,
    payload: GameUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin privilege: Update game parameters.
    """
    game_doc = await db.games.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not game_doc:
        raise HTTPException(status_code=404, detail="Game not found")

    update_data = payload.model_dump(exclude_unset=True)
    if update_data:
        await db.games.update_one({"_id": game_doc["_id"]}, {"$set": update_data})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPDATE_GAME",
        target=f"Game {game_doc['name']} ({id})",
        details=f"Updated game fields: {list(update_data.keys())}",
    )

    updated_doc = await db.games.find_one({"_id": game_doc["_id"]})
    gid = str(updated_doc.get("_id", updated_doc.get("id")))
    count = await db.tournaments.count_documents({"$or": [{"game_id": gid}, {"game_slug": updated_doc.get("slug")}]})
    return serialize_game_doc(updated_doc, count)

@router.post("/{id}/publish", response_model=GameResponse)
async def publish_game(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin privilege: One-click publish game from COMING_SOON to ACTIVE.
    """
    game_doc = await db.games.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not game_doc:
        raise HTTPException(status_code=404, detail="Game not found")

    await db.games.update_one(
        {"_id": game_doc["_id"]},
        {"$set": {"status": "ACTIVE"}}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="PUBLISH_GAME",
        target=f"Game {game_doc['name']} ({id})",
        details=f"Published game {game_doc['name']} to ACTIVE status",
    )

    updated = await db.games.find_one({"_id": game_doc["_id"]})
    gid = str(updated.get("_id", updated.get("id")))
    count = await db.tournaments.count_documents({"$or": [{"game_id": gid}, {"game_slug": updated.get("slug")}]})
    return serialize_game_doc(updated, count)

@router.delete("/{id}")
async def delete_game(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin privilege: Delete game title if no tournaments depend on it.
    """
    game_doc = await db.games.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not game_doc:
        raise HTTPException(status_code=404, detail="Game not found")

    gid = str(game_doc.get("_id", game_doc.get("id")))
    count = await db.tournaments.count_documents({"$or": [{"game_id": gid}, {"game_slug": game_doc.get("slug")}]})
    if count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete game '{game_doc['name']}': {count} tournament(s) are linked to it",
        )

    await db.games.delete_one({"_id": game_doc["_id"]})
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="DELETE_GAME",
        target=f"Game {game_doc['name']} ({id})",
        details=f"Deleted game title {game_doc['name']}",
    )
    return {"status": "success", "message": f"Game '{game_doc['name']}' deleted"}

