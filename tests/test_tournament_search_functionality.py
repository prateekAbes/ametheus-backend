import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin, require_organizer

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_123",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_tournament_search_backend_functionality(client, mock_db):
    # Seed games
    await mock_db.games.insert_many([
        {
            "_id": "game_val",
            "id": "game_val",
            "slug": "valorant",
            "name": "Valorant",
            "short_name": "VAL",
            "is_team_game": True,
        },
        {
            "_id": "game_bgmi",
            "id": "game_bgmi",
            "slug": "bgmi",
            "name": "Battlegrounds Mobile India",
            "short_name": "BGMI",
            "is_team_game": True,
        },
        {
            "_id": "game_codm",
            "id": "game_codm",
            "slug": "codm",
            "name": "Call of Duty: Mobile",
            "short_name": "CODM",
            "is_team_game": True,
        },
    ])

    # Seed tournaments
    await mock_db.tournaments.insert_many([
        {
            "_id": "t1",
            "id": "t1",
            "slug": "val-championship-2026",
            "title": "Valorant Championship 2026",
            "game_id": "game_val",
            "status": "REGISTRATION_OPEN",
            "format": "5v5 Single Elimination",
            "description": "The premier national Valorant championship.",
            "max_teams": 16,
            "is_dummy": False,
        },
        {
            "_id": "t2",
            "id": "t2",
            "slug": "val-ascent-series",
            "title": "Ascent Series",
            "game_id": "game_val",
            "status": "REGISTRATION_OPEN",
            "format": "BO3",
            "description": "Weekly competitive scrims and ladder matches.",
            "max_teams": 32,
            "is_dummy": False,
        },
        {
            "_id": "t3",
            "id": "t3",
            "slug": "bgmi-monsoon-cup",
            "title": "BGMI Monsoon Cup S2",
            "game_id": "game_bgmi",
            "status": "REGISTRATION_OPEN",
            "format": "Squad TPP",
            "description": "Monsoon championship series for elite squads.",
            "max_teams": 25,
            "is_dummy": False,
        },
        {
            "_id": "t4",
            "id": "t4",
            "slug": "codm-frontline-clash",
            "title": "Frontline Clash",
            "game_id": "game_codm",
            "status": "UPCOMING",
            "format": "Double Elimination",
            "description": "Mobile FPS warfare.",
            "max_teams": 16,
            "is_dummy": False,
        },
    ])

    # 1. Search full title
    res = await client.get("/api/v1/tournaments?search=Valorant Championship 2026")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["slug"] == "val-championship-2026"

    # 2. Search partial title ("champ")
    res = await client.get("/api/v1/tournaments?search=champ")
    assert res.status_code == 200
    data = res.json()
    slugs = [t["slug"] for t in data]
    assert "val-championship-2026" in slugs
    assert "bgmi-monsoon-cup" in slugs  # contains "championship" in description

    # 3. Case-insensitivity ("VALORANT" vs "valorant")
    res_upper = await client.get("/api/v1/tournaments?search=VALORANT")
    res_lower = await client.get("/api/v1/tournaments?search=valorant")
    assert res_upper.status_code == 200 and res_lower.status_code == 200
    data_upper = res_upper.json()
    data_lower = res_lower.json()
    assert len(data_upper) == 2  # matches both t1 and t2 because both are Valorant
    assert len(data_lower) == 2
    assert {t["slug"] for t in data_upper} == {"val-championship-2026", "val-ascent-series"}

    # 4. Leading / trailing whitespace
    res_ws = await client.get("/api/v1/tournaments?search=%20%20%20bgmi%20%20%20")
    assert res_ws.status_code == 200
    assert len(res_ws.json()) == 1
    assert res_ws.json()[0]["slug"] == "bgmi-monsoon-cup"

    # 5. Non-matching search returns 0 results
    res_empty = await client.get("/api/v1/tournaments?search=nonexistentxyz123")
    assert res_empty.status_code == 200
    assert len(res_empty.json()) == 0

    # 6. Combining search with game filter
    res_combined = await client.get("/api/v1/tournaments?game=valorant&search=ascent")
    assert res_combined.status_code == 200
    assert len(res_combined.json()) == 1
    assert res_combined.json()[0]["slug"] == "val-ascent-series"

    # 7. Combining search with conflicting game filter returns 0
    res_conflict = await client.get("/api/v1/tournaments?game=bgmi&search=ascent")
    assert res_conflict.status_code == 200
    assert len(res_conflict.json()) == 0

    # 8. Empty search parameter returns all active tournaments
    res_all = await client.get("/api/v1/tournaments?search=")
    assert res_all.status_code == 200
    assert len(res_all.json()) >= 3
