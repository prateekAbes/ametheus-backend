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
    if _db is not None:
        return _db
    if _client is None or (hasattr(_client, "get_io_loop") and _client.get_io_loop().is_closed()):
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
        # Case-insensitive username uniqueness — used for admin reservation and deduplication
        await db.users.create_index("username_lower", unique=True, sparse=True)
        await db.users.create_index("clerk_user_id", unique=True, sparse=True)
        await db.users.create_index("is_dummy")

        # Games
        await db.games.create_index("slug", unique=True)
        await db.games.create_index("is_dummy")

        # Teams
        await db.teams.create_index("invite_code", unique=True)
        await db.teams.create_index("captain_id")
        await db.teams.create_index("game_id")
        await db.teams.create_index([("game_id", 1), ("captain_id", 1)])
        await db.teams.create_index([("game_id", 1), ("members.user_id", 1)])
        await db.teams.create_index([("game_id", 1), ("substitutes.user_id", 1)])
        await db.teams.create_index("is_dummy")

        # Tournament Registrations
        await db.tournament_registrations.create_index(
            [("tournament_id", 1), ("team_id", 1)],
            unique=True,
            partialFilterExpression={"team_id": {"$type": "string"}},
            name="tournament_team_unique",
        )
        await db.tournament_registrations.create_index(
            [("tournament_id", 1), ("player_id", 1)],
            unique=True,
            partialFilterExpression={"player_id": {"$type": "string"}},
            name="tournament_player_unique",
        )
        await db.tournament_registrations.create_index("player_id")
        await db.tournament_registrations.create_index("team_id")
        await db.tournament_registrations.create_index("tournament_id")

        # UPI Accounts
        await db.upi_accounts.create_index("code", unique=True)
        await db.upi_accounts.create_index("status")
        await db.upi_accounts.create_index("is_dummy")

        # Game Payments
        try:
            await db.game_payments.drop_index("utr_1_payment_type_1")
        except Exception:
            pass
        # Clean any legacy 0-rupee payment records
        try:
            await db.game_payments.delete_many({"amount_paise": 0})
        except Exception:
            pass
        await db.game_payments.create_index(
            "utr",
            unique=True,
            partialFilterExpression={"utr": {"$type": "string"}},
            name="utr_unique_string",
        )
        await db.game_payments.create_index("status")
        await db.game_payments.create_index("user_id")
        await db.game_payments.create_index("game_id")
        await db.game_payments.create_index("team_id")
        await db.game_payments.create_index("tournament_id")
        await db.game_payments.create_index("registration_id")
        await db.game_payments.create_index("payment_account_code")
        await db.game_payments.create_index("is_dummy")

        # Matches
        await db.matches.create_index("tournament_id")
        await db.matches.create_index("status")
        await db.matches.create_index("scheduled_at")
        await db.matches.create_index([("tournament_id", 1), ("stage", 1)])

        # Wallet Ledger
        await db.wallet_ledger.create_index("user_id")
        await db.wallet_ledger.create_index("created_at")
        await db.wallet_ledger.create_index([("user_id", 1), ("created_at", -1)])
        await db.wallet_ledger.create_index("is_dummy")

        # Payouts
        await db.payouts.create_index("user_id")
        await db.payouts.create_index("status")
        await db.payouts.create_index("game_id")
        await db.payouts.create_index("is_dummy")
        await db.payouts.create_index("is_archived")

        # Audit Logs & Notifications
        await db.audit_logs.create_index("created_at")
        await db.audit_logs.create_index("is_archived")
        await db.notifications.create_index("user_id")
        await db.notifications.create_index([("user_id", 1), ("is_read", 1)])
        await db.notifications.create_index("is_archived")

        # Support Tickets & Payments Archival Indexes
        await db.game_payments.create_index("is_archived")
        await db.wallet_ledger.create_index("is_archived")
        await db.support_tickets.create_index("is_archived")

        # Data Migration: Rule 5 & 11 - Normalize tournament status and match live links
        try:
            await db.tournaments.update_many(
                {"status": {"$in": ["LIVE", "ONGOING", "IN_PROGRESS"]}},
                {"$set": {"status": "REGISTRATION_CLOSED"}}
            )
            await db.matches.update_many(
                {"youtube_live_url": {"$exists": False}, "live_link": {"$ne": None}},
                [{"$set": {"youtube_live_url": "$live_link"}}]
            )
        except Exception:
            pass
    except Exception as e:
        print(f"[AMETHEUS MongoDB Warning] Index initialization error: {e}")
