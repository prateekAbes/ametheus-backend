import re
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

DEFAULT_GAMES = [
    # Active games
    {
        "slug": "bgmi",
        "name": "Battlegrounds Mobile India",
        "category": "battle-royale",
        "genre_name": "Battle Royale",
        "platform": "Mobile (Android / iOS)",
        "tag": "BGMI",
        "is_team_game": True,
        "supported_game_modes": ["SOLO", "DUO", "SQUAD"],
        "min_team_size": 4,
        "max_team_size": 4,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 25000,
        "banner_url": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop",
        "description": "Squad battle royale on Erangel and Miramar. Shrinking blue zone, airdrops, and high-tension tactical survival.",
        "status": "ACTIVE",
        "accent_color": "#E56A10",
    },
    {
        "slug": "valorant",
        "name": "Valorant",
        "category": "tactical-fps",
        "genre_name": "Tactical FPS",
        "platform": "PC",
        "tag": "VALORANT",
        "is_team_game": True,
        "supported_game_modes": ["SQUAD"],
        "min_team_size": 5,
        "max_team_size": 5,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 50000,
        "banner_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
        "description": "5v5 character-based tactical shooter. Precise gunplay paired with unique agent abilities in high-stakes Spike defusal.",
        "status": "ACTIVE",
        "accent_color": "#FF4655",
    },
    {
        "slug": "free-fire",
        "name": "Free Fire Max",
        "category": "battle-royale",
        "genre_name": "Battle Royale",
        "platform": "Mobile (Android / iOS)",
        "tag": "FF MAX",
        "is_team_game": True,
        "supported_game_modes": ["SOLO", "DUO", "SQUAD"],
        "min_team_size": 4,
        "max_team_size": 4,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 20000,
        "banner_url": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1579783902614-a3fb3927b675?q=80&w=300&auto=format&fit=crop",
        "description": "Fast-paced 50-player mobile survival shooter with 10-minute matches, gloo walls, and character skill builds.",
        "status": "ACTIVE",
        "accent_color": "#FFAA00",
    },
    {
        "slug": "cod-mobile",
        "name": "Call of Duty: Mobile",
        "category": "tactical-fps",
        "genre_name": "Tactical FPS",
        "platform": "Mobile (Android / iOS)",
        "tag": "CODM",
        "is_team_game": True,
        "supported_game_modes": ["SOLO", "SQUAD"],
        "min_team_size": 5,
        "max_team_size": 5,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 20000,
        "banner_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop",
        "description": "Iconic Call of Duty maps and multiplayer modes including Search & Destroy, Hardpoint, and Domination on mobile.",
        "status": "ACTIVE",
        "accent_color": "#00F0FF",
    },
    {
        "slug": "chess",
        "name": "Chess Blitz Arenas",
        "category": "strategy",
        "genre_name": "Strategy & Board",
        "platform": "Cross-Platform",
        "tag": "CHESS",
        "is_team_game": False,
        "supported_game_modes": ["SOLO"],
        "min_team_size": 1,
        "max_team_size": 1,
        "max_substitutes": 0,
        "entry_fee_inr": 0,
        "prize_pool_inr": 10000,
        "banner_url": "https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1586165368502-1bad197a6461?q=80&w=300&auto=format&fit=crop",
        "description": "Blitz, Rapid, and Bullet chess arenas. Pure tactical calculation, Swiss bracket rounds, and FIDE-rated Elo mechanics.",
        "status": "ACTIVE",
        "accent_color": "#3EF2FF",
    },
    # Coming Soon games
    {
        "slug": "counter-strike-2",
        "name": "Counter-Strike 2",
        "category": "tactical-fps",
        "genre_name": "Tactical FPS",
        "platform": "PC",
        "tag": "CS2",
        "is_team_game": True,
        "supported_game_modes": ["SQUAD"],
        "min_team_size": 5,
        "max_team_size": 5,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop",
        "description": "Source 2 sub-tick responsiveness, responsive smokes, and classic competitive Bomb Defusal arenas.",
        "status": "COMING_SOON",
        "accent_color": "#DE9B35",
        "expected_release_date": "Q4 2026",
        "announcement_text": "Sub-tick tournament lobbies coming soon",
    },
    {
        "slug": "rocket-league",
        "name": "Rocket League",
        "category": "sports",
        "genre_name": "Sports & Racing",
        "platform": "Cross-Platform",
        "tag": "RL",
        "is_team_game": True,
        "supported_game_modes": ["DUO", "SQUAD"],
        "min_team_size": 3,
        "max_team_size": 3,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=300&auto=format&fit=crop",
        "description": "High-powered hybrid of arcade-style soccer and vehicular mayhem. Aerial rotations, wall pinches, and 3v3 arenas.",
        "status": "COMING_SOON",
        "accent_color": "#0088FF",
        "expected_release_date": "Q4 2026",
        "announcement_text": "Supersonic acrobatic tournament brackets",
    },
    {
        "slug": "clash-royale",
        "name": "Clash Royale",
        "category": "strategy",
        "genre_name": "Strategy & Board",
        "platform": "Mobile (Android / iOS)",
        "tag": "CR",
        "is_team_game": False,
        "supported_game_modes": ["SOLO", "DUO"],
        "min_team_size": 1,
        "max_team_size": 1,
        "max_substitutes": 0,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1586165368502-1bad197a6461?q=80&w=300&auto=format&fit=crop",
        "description": "Real-time head-to-head tower rush battles combining card collection, tower defense, and multiplayer online battle arena.",
        "status": "COMING_SOON",
        "accent_color": "#FFBB00",
        "expected_release_date": "Q4 2026",
        "announcement_text": "Tournament standard 1v1 duel brackets",
    },
    {
        "slug": "brawl-stars",
        "name": "Brawl Stars",
        "category": "battle-royale",
        "genre_name": "Battle Royale",
        "platform": "Mobile (Android / iOS)",
        "tag": "BS",
        "is_team_game": True,
        "supported_game_modes": ["SOLO", "DUO", "SQUAD"],
        "min_team_size": 3,
        "max_team_size": 3,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1579783902614-a3fb3927b675?q=80&w=300&auto=format&fit=crop",
        "description": "Fast-paced 3v3 multiplayer and battle royale combat across Gem Grab, Brawl Ball, and Knockout modes.",
        "status": "COMING_SOON",
        "accent_color": "#FF5500",
        "expected_release_date": "Q4 2026",
        "announcement_text": "3v3 mobile tournament integration",
    },
    {
        "slug": "league-of-legends",
        "name": "League of Legends",
        "category": "strategy",
        "genre_name": "Strategy & Board",
        "platform": "PC",
        "tag": "LOL",
        "is_team_game": True,
        "supported_game_modes": ["SQUAD"],
        "min_team_size": 5,
        "max_team_size": 5,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
        "description": "Summoner's Rift 5v5 competitive MOBA. Team synergy, lane macro, dragon soul control, and Baron Nashor teamfights.",
        "status": "COMING_SOON",
        "accent_color": "#C89B3C",
        "expected_release_date": "Q4 2026",
        "announcement_text": "5v5 Summoner's Rift competitive ladder",
    },
    {
        "slug": "smash-karts",
        "name": "Smash Karts",
        "category": "sports",
        "genre_name": "Sports & Racing",
        "platform": "Cross-Platform",
        "tag": "SMASH",
        "is_team_game": False,
        "supported_game_modes": ["SOLO", "DUO", "SQUAD"],
        "min_team_size": 1,
        "max_team_size": 1,
        "max_substitutes": 0,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop",
        "description": "3D multiplayer kart battle arena. Power-ups, rocket launchers, and chaotic fast-paced round-robin matches.",
        "status": "COMING_SOON",
        "accent_color": "#FF2D55",
        "expected_release_date": "Q4 2026",
        "announcement_text": "Browser-based multiplayer tournament cups",
    },
    {
        "slug": "mini-militia",
        "name": "Mini Militia (Doodle Army 2)",
        "category": "tactical-fps",
        "genre_name": "Tactical FPS",
        "platform": "Mobile (Android / iOS)",
        "tag": "MILITIA",
        "is_team_game": True,
        "supported_game_modes": ["DUO", "SQUAD"],
        "min_team_size": 3,
        "max_team_size": 3,
        "max_substitutes": 1,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop",
        "description": "Intense multiplayer combat with up to 6 players online in 2D cartoon themed cross between Soldat and Halo.",
        "status": "COMING_SOON",
        "accent_color": "#4CD964",
        "expected_release_date": "Q4 2026",
        "announcement_text": "Custom room 3v3 team deathmatches",
    },
    {
        "slug": "mirror-blades",
        "name": "Mirror Blades",
        "category": "tactical-fps",
        "genre_name": "Tactical FPS",
        "platform": "PC",
        "tag": "BLADES",
        "is_team_game": False,
        "supported_game_modes": ["SOLO"],
        "min_team_size": 1,
        "max_team_size": 1,
        "max_substitutes": 0,
        "entry_fee_inr": 0,
        "prize_pool_inr": 0,
        "banner_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
        "description": "Reflexive sword dueling and precision parry arenas in futuristic neon cyber-dojos.",
        "status": "COMING_SOON",
        "accent_color": "#5856D6",
        "expected_release_date": "Q4 2026",
        "announcement_text": "1v1 melee tournament duels",
    },
]

async def seed_games_if_needed(db: AsyncIOMotorDatabase):
    count = await db.games.count_documents({})
    if count == 0:
        for dg in DEFAULT_GAMES:
            g_doc = Game(**dg)
            mongo_doc = g_doc.to_mongo()
            if dg.get("genre_name"):
                genre_match = await db.genres.find_one({"name": {"$regex": f"^{re.escape(dg['genre_name'])}$", "$options": "i"}})
                if genre_match:
                    mongo_doc["genre_id"] = str(genre_match.get("_id", genre_match.get("id")))
                    mongo_doc["genre_name"] = genre_match.get("name")
            await db.games.insert_one(mongo_doc)
    else:
        # Non-destructive backfill for existing games in database lacking supported_game_modes
        existing_without_modes = await db.games.find({"supported_game_modes": {"$exists": False}}).to_list(length=200)
        for eg in existing_without_modes:
            min_s = eg.get("min_team_size", 1)
            max_s = eg.get("max_team_size", 5)
            is_team = eg.get("is_team_game", True)
            cat = str(eg.get("category", "")).lower()
            if not is_team or (min_s == 1 and max_s == 1):
                s_modes = ["SOLO"]
            elif "battle-royale" in cat or "br" in cat:
                s_modes = ["SOLO", "DUO", "SQUAD"]
            else:
                s_modes = ["SQUAD"]
            await db.games.update_one({"_id": eg["_id"]}, {"$set": {"supported_game_modes": s_modes}})

def normalize_category_slug(category: Optional[str]) -> str:
    if not category:
        return "pc"
    c = category.strip().lower().replace(" ", "-").replace("_", "-")
    aliases = {
        "br": "battle-royale",
        "first-person-shooter": "fps",
        "sports-racing": "sports",
        "racing": "sports",
        "strategy-card": "strategy",
        "card": "strategy",
    }
    return aliases.get(c, c)

def sanitize_game_images(slug: str, banner: Optional[str], logo: Optional[str]):
    # Never inject fallback images into game responses. Empty artwork must remain empty.
    b = (banner or "") if (banner and "aida-public" not in banner and "_Banner" not in banner) else ""
    l = (logo or "") if (logo and "aida-public" not in logo and "_Logo" not in logo) else ""
    return b, l

def serialize_game_doc(g: dict, teams_count: int = 0) -> GameResponse:
    gid = str(g.get("_id", g.get("id")))
    raw_banner = g.get("banner_url") or ""
    raw_logo = g.get("logo_url") or ""
    b_url, l_url = sanitize_game_images(g.get("slug", ""), raw_banner, raw_logo)
    poster = g.get("poster_url") or ""
    if "aida-public" in poster:
        poster = ""
    supported_modes = g.get("supported_game_modes")
    if not supported_modes:
        min_s = g.get("min_team_size", 1)
        max_s = g.get("max_team_size", 5)
        is_team = g.get("is_team_game", True)
        cat = str(g.get("category", "")).lower()
        if not is_team or (min_s == 1 and max_s == 1):
            supported_modes = ["SOLO"]
        elif "battle-royale" in cat or "br" in cat:
            supported_modes = ["SOLO", "DUO", "SQUAD"]
        else:
            supported_modes = ["SQUAD"]
    else:
        supported_modes = [str(m).upper().strip() for m in supported_modes if str(m).upper().strip() in {"SOLO", "DUO", "SQUAD"}]
        if not supported_modes:
            supported_modes = ["SQUAD"]

    return GameResponse(
        id=gid,
        slug=g["slug"],
        name=g["name"],
        category=g.get("category", "pc"),
        platform=g.get("platform", "PC"),
        tag=g.get("tag", "ESPORTS"),
        is_team_game=g.get("is_team_game", True),
        supported_game_modes=supported_modes,
        min_team_size=g.get("min_team_size", 1),
        max_team_size=g.get("max_team_size", 5),
        max_substitutes=g.get("max_substitutes", 1),
        entry_fee_inr=g.get("entry_fee_inr", 0),
        prize_pool_inr=g.get("prize_pool_inr", 0),
        play_url=g.get("play_url"),
        poster_url=poster,
        banner_url=b_url,
        banner_tablet_url=g.get("banner_tablet_url"),
        banner_mobile_url=g.get("banner_mobile_url"),
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
        genre_id=g.get("genre_id"),
        genre_name=g.get("genre_name"),
        genre=g.get("genre") or g.get("genre_name"),
        media_config=g.get("media_config", {}),
    )

@router.get("", response_model=List[GameResponse])
async def list_games(
    status: Optional[str] = Query(None, description="Filter games by status e.g. ACTIVE, COMING_SOON"),
    genre_id: Optional[str] = Query(None, description="Filter games by genre ID or slug"),
    category: Optional[str] = Query(None, description="Filter games by category"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await seed_games_if_needed(db)
    query = {"is_dummy": {"$ne": True}}
    if status:
        query["status"] = status.upper()
    if genre_id:
        query["$or"] = [{"genre_id": genre_id}, {"genre_name": genre_id}, {"category": genre_id}]
    elif category:
        c_norm = normalize_category_slug(category)
        query["$or"] = [{"category": c_norm}, {"genre_name": category}, {"genre_id": category}]
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
    await seed_games_if_needed(db)
    from bson import ObjectId
    or_clauses = [{"slug": slug}, {"slug": slug.lower()}, {"_id": slug}, {"id": slug}]
    if ObjectId.is_valid(slug):
        try:
            or_clauses.append({"_id": ObjectId(slug)})
        except Exception:
            pass
    g = await db.games.find_one({"$or": or_clauses})
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

    g_name = payload.genre_name or payload.genre
    if payload.genre_id and not g_name:
        genre_doc = await db.genres.find_one({"$or": [{"_id": payload.genre_id}, {"id": payload.genre_id}, {"slug": payload.genre_id}]})
        if genre_doc:
            g_name = genre_doc.get("name")

    game = Game(
        slug=payload.slug,
        name=payload.name,
        category=normalize_category_slug(payload.category),
        genre_id=payload.genre_id,
        genre_name=g_name,
        platform=payload.platform,
        tag=payload.tag,
        is_team_game=payload.is_team_game,
        supported_game_modes=payload.supported_game_modes,
        min_team_size=payload.min_team_size,
        max_team_size=payload.max_team_size,
        max_substitutes=payload.max_substitutes,
        entry_fee_inr=payload.entry_fee_inr,
        prize_pool_inr=payload.prize_pool_inr,
        play_url=payload.play_url,
        poster_url=payload.poster_url,
        banner_url=payload.banner_url or "",
        banner_tablet_url=payload.banner_tablet_url,
        banner_mobile_url=payload.banner_mobile_url,
        logo_url=payload.logo_url or "",
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

    fields_set = payload.model_fields_set
    update_data = {}

    for k in fields_set:
        v = getattr(payload, k)
        update_data[k] = v

    if "category" in update_data and update_data["category"] is not None:
        update_data["category"] = normalize_category_slug(update_data["category"])

    if "genre_id" in update_data:
        if not update_data["genre_id"]:
            update_data["genre_id"] = None
            update_data["genre_name"] = None
        elif not update_data.get("genre_name"):
            genre_doc = await db.genres.find_one({"$or": [{"_id": update_data["genre_id"]}, {"id": update_data["genre_id"]}, {"slug": update_data["genre_id"]}]})
            if genre_doc:
                update_data["genre_name"] = genre_doc.get("name")
    if "genre" in update_data and update_data["genre"] is not None and not update_data.get("genre_name"):
        update_data["genre_name"] = update_data["genre"]

    # Sync canonical artwork slots from media_config when provided
    if "media_config" in fields_set:
        mc = update_data.get("media_config")
        if isinstance(mc, dict):
            # Sync catalog_card / coming_soon_catalog_card asset_url to poster_url
            if "catalog_card" in mc and isinstance(mc["catalog_card"], dict):
                update_data["poster_url"] = mc["catalog_card"].get("asset_url") or ""
            elif "coming_soon_catalog_card" in mc and isinstance(mc["coming_soon_catalog_card"], dict) and (game.get("status") in ["COMING_SOON", "MAINTENANCE"]):
                update_data["poster_url"] = mc["coming_soon_catalog_card"].get("asset_url") or ""

            # Sync banner asset_url to banner_url
            if "banner" in mc and isinstance(mc["banner"], dict):
                update_data["banner_url"] = mc["banner"].get("asset_url") or ""

            # Sync logo asset_url to logo_url
            if "logo" in mc and isinstance(mc["logo"], dict):
                update_data["logo_url"] = mc["logo"].get("asset_url") or ""

    # Ensure explicitly set empty/null image fields normalize to empty strings
    for img_field in ["poster_url", "banner_url", "logo_url", "banner_tablet_url", "banner_mobile_url", "hero_media_url"]:
        if img_field in fields_set and update_data.get(img_field) is None:
            update_data[img_field] = ""

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
