import uuid
from typing import Optional, Any, Dict, List
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.core.config import settings

# Global Motor client and database references
_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None

def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None or (hasattr(_client, "get_io_loop") and _client.get_io_loop().is_closed()):
        _client = AsyncIOMotorClient(
            settings.MONGODB_URI,
            serverSelectionTimeoutMS=5000,
        )
    return _client

def get_database() -> AsyncIOMotorDatabase:
    global _db, _client
    if _db is None or _client is None or (hasattr(_client, "get_io_loop") and _client.get_io_loop().is_closed()):
        client = get_client()
        _db = client[settings.MONGODB_DB_NAME]
    return _db

def set_database(database: AsyncIOMotorDatabase):
    """Allows test harnesses (e.g. mongomock-motor) to inject an in-memory database."""
    global _db
    _db = database

async def get_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency for accessing the MongoDB database."""
    return get_database()

def doc_to_dict(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Converts a MongoDB document to a dict with 'id' mapped from '_id'."""
    if doc is None:
        return None
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d["_id"])
    return d

def docs_to_list(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Converts a list of MongoDB documents into standard dicts with mapped 'id'."""
    return [doc_to_dict(d) for d in docs if d is not None]

def generate_id() -> str:
    return uuid.uuid4().hex

async def init_db_indexes(db: AsyncIOMotorDatabase):
    """Initializes performance and uniqueness indexes on all MongoDB collections."""
    try:
        # Users
        await db.users.create_index("email", unique=True)
        await db.users.create_index("username", unique=True)
        await db.users.create_index("clerk_user_id", unique=True, sparse=True)

        # Games
        await db.games.create_index("slug", unique=True)

        # Tournaments
        await db.tournaments.create_index("slug", unique=True)
        await db.tournaments.create_index("game_id")
        await db.tournaments.create_index("organizer_id")
        await db.tournaments.create_index("status")

        # Teams
        await db.teams.create_index("invite_code", unique=True)
        await db.teams.create_index("captain_id")
        await db.teams.create_index("game_id")

        # Tournament Registrations
        await db.tournament_registrations.create_index("tournament_id")
        await db.tournament_registrations.create_index("team_id")
        await db.tournament_registrations.create_index("player_id")

        # Matches
        await db.matches.create_index("tournament_id")
        await db.matches.create_index("status")

        # Admin Assignments
        await db.tournament_admin_assignments.create_index([("admin_user_id", 1), ("tournament_id", 1)])

        # Audit Logs
        await db.audit_logs.create_index("created_at")

        # Disputes
        await db.disputes.create_index("tournament_id")
        await db.disputes.create_index("match_id")

        # Notifications
        await db.notifications.create_index("user_id")
        await db.notifications.create_index([("user_id", 1), ("is_read", 1)])
    except Exception as e:
        print(f"[AMETHEUS MongoDB Warning] Index initialization error: {e}")

