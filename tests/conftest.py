import sys
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from mongomock_motor import AsyncMongoMockClient

from app.core.database import get_db, set_database, get_database, init_db_indexes
from app.core.security import create_access_token
from tests.seed import populate_seed_data
from app.main import app

# Ensure identical module reference when imported as 'tests.conftest' or 'conftest'
sys.modules["tests.conftest"] = sys.modules[__name__]
sys.modules["conftest"] = sys.modules[__name__]

if not hasattr(get_database, "_mock_db"):
    _client = AsyncMongoMockClient()
    _mock_db = _client["test_ametheus"]
    set_database(_mock_db)
    get_database._mock_db = _mock_db

mock_db = get_database._mock_db

async def override_get_db():
    yield mock_db

app.dependency_overrides[get_db] = override_get_db

def make_auth_headers(user_id: str) -> dict:
    token = create_access_token({"sub": user_id})
    return {"Authorization": f"Bearer {token}"}

class MongoTestSession:
    def __init__(self, db=None):
        self.db = db if db is not None else get_database._mock_db
        self.pending = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.commit()

    def add(self, obj):
        self.pending.append(obj)

    def add_all(self, objs):
        self.pending.extend(objs)

    async def flush(self):
        await self.commit()

    async def refresh(self, obj):
        pass

    async def commit(self):
        for obj in self.pending:
            if hasattr(obj, "to_mongo"):
                d = obj.to_mongo()
            elif hasattr(obj, "model_dump"):
                d = obj.model_dump()
                if "id" in d and "_id" not in d:
                    d["_id"] = d.pop("id")
            elif isinstance(obj, dict):
                d = dict(obj)
                if "id" in d and "_id" not in d:
                    d["_id"] = d.pop("id")
            else:
                d = obj.__dict__.copy()
                if "id" in d and "_id" not in d:
                    d["_id"] = d.pop("id")

            cls_name = obj.__class__.__name__.lower()
            if "member" in cls_name:
                tid = getattr(obj, "team_id", None)
                if tid:
                    await self.db.teams.update_one(
                        {"$or": [{"_id": tid}, {"id": tid}]},
                        {"$push": {"members": d}}
                    )
                continue
            elif "user" in cls_name:
                col = self.db.users
            elif "game" in cls_name:
                col = self.db.games
            elif "team" in cls_name:
                col = self.db.teams
            elif "tournament" in cls_name and "registration" not in cls_name and "admin" not in cls_name:
                col = self.db.tournaments
            elif "registration" in cls_name:
                col = self.db.tournament_registrations
            elif "match" in cls_name:
                col = self.db.matches
            elif "dispute" in cls_name:
                col = self.db.disputes
            elif "notification" in cls_name:
                col = self.db.notifications
            elif "audit" in cls_name:
                col = self.db.audit_logs
            else:
                col = self.db.general

            target_id = d.get("_id", d.get("id"))
            await col.update_one({"_id": target_id}, {"$set": d}, upsert=True)
        self.pending = []

    async def execute(self, statement):
        class MockScalarResult:
            def __init__(self, items):
                self.items = items
            def scalars(self):
                return self
            def first(self):
                return self.items[0] if self.items else None
            def all(self):
                return self.items

        return MockScalarResult([])

def TestSessionLocal():
    return MongoTestSession(get_database._mock_db)

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    db = get_database._mock_db
    await init_db_indexes(db)
    await populate_seed_data(db)
    yield

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def db_session():
    yield get_database._mock_db
