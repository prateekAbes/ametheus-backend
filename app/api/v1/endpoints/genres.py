from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, require_organizer, require_super_admin
from app.models.user import User
from app.models.genre import Genre
from app.models.game import Game
from app.schemas import GenreResponse, GenreCreate, GenreUpdate, GameResponse
from app.services.audit import record_audit_log

router = APIRouter()
admin_router = APIRouter()

DEFAULT_GENRES = [
    {
        "name": "Battle Royale",
        "slug": "battle-royale",
        "description": "High-stakes multi-player survival battlegrounds with shrinking zones and strategic squad play.",
        "rotation_interval_seconds": 4,
        "display_order": 1,
        "banner_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "game_slugs": ["bgmi", "free-fire", "apex-legends"],
    },
    {
        "name": "Tactical FPS",
        "slug": "tactical-fps",
        "description": "Precision gunplay, agent abilities, and high-tension bomb defusal tournaments.",
        "rotation_interval_seconds": 4,
        "display_order": 2,
        "banner_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "game_slugs": ["valorant", "counter-strike-2"],
    },
    {
        "name": "Strategy & Board",
        "slug": "strategy",
        "description": "Intellectual mastery, classical turn-based calculation, and rapid blitz chess arenas.",
        "rotation_interval_seconds": 4,
        "display_order": 3,
        "banner_url": "https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200&auto=format&fit=crop",
        "game_slugs": ["chess"],
    },
]

async def seed_genres_if_needed(db: AsyncIOMotorDatabase):
    count = await db.genres.count_documents({})
    if count == 0:
        for dg in DEFAULT_GENRES:
            g_doc = Genre(
                name=dg["name"],
                slug=dg["slug"],
                description=dg["description"],
                rotation_interval_seconds=dg["rotation_interval_seconds"],
                display_order=dg["display_order"],
                banner_url=dg["banner_url"],
            )
            await db.genres.insert_one(g_doc.to_mongo())
            # Attach existing games
            for g_slug in dg.get("game_slugs", []):
                await db.games.update_many(
                    {"slug": g_slug},
                    {"$set": {"genre_id": g_doc.id, "genre_name": g_doc.name}}
                )

import re

async def enrich_genre_doc(g_doc: dict, db: AsyncIOMotorDatabase) -> GenreResponse:
    gid = str(g_doc.get("_id", g_doc.get("id")))
    slug = g_doc.get("slug", "")
    name = g_doc.get("name", "")

    # Fetch games belonging to this genre (by genre_id, name, or slug match across hyphens/underscores/case)
    slug_norm = slug.lower().replace("-", "_")
    slug_hyphen = slug.lower().replace("_", "-")
    cat_pattern = f"^({re.escape(slug)}|{re.escape(slug_norm)}|{re.escape(slug_hyphen)})$"

    games_cursor = db.games.find({
        "$or": [
            {"genre_id": gid},
            {"genre_name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}},
            {"category": {"$regex": cat_pattern, "$options": "i"}},
        ],
        "is_dummy": {"$ne": True},
    })
    games_raw = await games_cursor.to_list(length=50)
    games_resp: List[GameResponse] = []
    for gr in games_raw:
        teams_count = await db.teams.count_documents({"game_id": str(gr.get("_id", gr.get("id")))})
        from app.api.v1.endpoints.games import serialize_game_doc
        games_resp.append(serialize_game_doc(gr, teams_count))

    return GenreResponse(
        id=gid,
        slug=slug,
        name=g_doc.get("name", "Genre"),
        description=g_doc.get("description", ""),
        banner_url=g_doc.get("banner_url"),
        icon_url=g_doc.get("icon_url"),
        rotation_interval_seconds=g_doc.get("rotation_interval_seconds", 4),
        display_order=g_doc.get("display_order", 0),
        games=games_resp,
        games_count=len(games_resp),
        game_count=len(games_resp),
    )

@router.get("", response_model=List[GenreResponse])
async def list_genres(db: AsyncIOMotorDatabase = Depends(get_db)):
    """
    Public listing of all official game genres with their dynamically attached games.
    """
    await seed_genres_if_needed(db)
    cursor = db.genres.find({}).sort("display_order", 1)
    genre_docs = await cursor.to_list(length=100)
    return [await enrich_genre_doc(doc, db) for doc in genre_docs]

@router.get("/{genre_id_or_slug}", response_model=GenreResponse)
async def get_genre_detail(genre_id_or_slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    doc = await db.genres.find_one({
        "$or": [{"_id": genre_id_or_slug}, {"id": genre_id_or_slug}, {"slug": genre_id_or_slug}]
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Genre not found")
    return await enrich_genre_doc(doc, db)

# Admin Operations
@admin_router.get("", response_model=List[GenreResponse])
async def list_admin_genres(
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    await seed_genres_if_needed(db)
    cursor = db.genres.find({}).sort("display_order", 1)
    genre_docs = await cursor.to_list(length=100)
    return [await enrich_genre_doc(doc, db) for doc in genre_docs]

@admin_router.post("", response_model=GenreResponse, status_code=status.HTTP_201_CREATED)
async def create_genre(
    payload: GenreCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin creates a new authoritative Genre.
    Guarantees no duplicate genre name or slug.
    """
    name_clean = payload.name.strip()
    slug_clean = payload.slug.strip().lower() if payload.slug else name_clean.lower().replace(" ", "-").replace("_", "-")

    existing = await db.genres.find_one({
        "$or": [
            {"name": {"$regex": f"^{name_clean}$", "$options": "i"}},
            {"slug": slug_clean},
        ]
    })
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f'Genre "{name_clean}" already exists. Do not create duplicate genres.'
        )

    genre = Genre(
        name=name_clean,
        slug=slug_clean,
        description=payload.description or "",
        banner_url=payload.banner_url,
        icon_url=payload.icon_url,
        rotation_interval_seconds=payload.rotation_interval_seconds or 4,
        display_order=payload.display_order or 0,
    )
    await db.genres.insert_one(genre.to_mongo())

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="CREATE_GENRE",
        target=f"Genre {genre.name}",
        details=f"Slug: {genre.slug}",
    )

    return await enrich_genre_doc(genre.to_mongo(), db)

@admin_router.patch("/{id}", response_model=GenreResponse)
async def update_genre(
    id: str,
    payload: GenreUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.genres.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Genre not found")

    update_fields = {}
    if payload.name is not None:
        name_clean = payload.name.strip()
        # Check uniqueness against other genres
        dup = await db.genres.find_one({
            "_id": {"$ne": doc["_id"]},
            "name": {"$regex": f"^{name_clean}$", "$options": "i"}
        })
        if dup:
            raise HTTPException(status_code=400, detail=f'Another genre named "{name_clean}" already exists.')
        update_fields["name"] = name_clean

    if payload.slug is not None:
        slug_clean = payload.slug.strip().lower()
        dup = await db.genres.find_one({
            "_id": {"$ne": doc["_id"]},
            "slug": slug_clean
        })
        if dup:
            raise HTTPException(status_code=400, detail=f'Another genre with slug "{slug_clean}" already exists.')
        update_fields["slug"] = slug_clean

    if payload.description is not None:
        update_fields["description"] = payload.description
    if payload.banner_url is not None:
        update_fields["banner_url"] = payload.banner_url
    if payload.icon_url is not None:
        update_fields["icon_url"] = payload.icon_url
    if payload.rotation_interval_seconds is not None:
        update_fields["rotation_interval_seconds"] = payload.rotation_interval_seconds
    if payload.display_order is not None:
        update_fields["display_order"] = payload.display_order

    if update_fields:
        update_fields["updated_at"] = datetime.utcnow()
        await db.genres.update_one({"_id": doc["_id"]}, {"$set": update_fields})

    refreshed = await db.genres.find_one({"_id": doc["_id"]})
    return await enrich_genre_doc(refreshed, db)

@admin_router.delete("/{id}")
async def delete_genre(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.genres.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Genre not found")

    gid = str(doc.get("_id", doc.get("id")))
    # Unlink games from this genre
    await db.games.update_many(
        {"$or": [{"genre_id": gid}, {"genre_name": doc.get("name")}]},
        {"$unset": {"genre_id": "", "genre_name": ""}}
    )

    await db.genres.delete_one({"_id": doc["_id"]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="DELETE_GENRE",
        target=f"Genre {doc.get('name')}",
        details=f"Unlinked associated games",
    )

    return {"status": "success", "message": f'Genre "{doc.get("name")}" deleted successfully.'}
