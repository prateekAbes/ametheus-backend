import pytest
from app.core.config import Settings

@pytest.mark.asyncio
async def test_health_endpoint(client):
    res = await client.get('/health')
    assert res.status_code == 200
    data = res.json()
    assert data['status'] == 'ok'
    assert data['service'] == 'ametheus-backend'

def test_database_url_render_sanitization():
    # Render postgres:// -> postgresql+asyncpg://
    s1 = Settings(DATABASE_URL='postgres://user:pass@ep-cool-db.singapore.render.com/ametheus?sslmode=require')
    assert s1.DATABASE_URL == 'postgresql+asyncpg://user:pass@ep-cool-db.singapore.render.com/ametheus'

    # Standard postgresql:// -> postgresql+asyncpg://
    s2 = Settings(DATABASE_URL='postgresql://user:pass@ep-cool-db.singapore.render.com/ametheus')
    assert s2.DATABASE_URL == 'postgresql+asyncpg://user:pass@ep-cool-db.singapore.render.com/ametheus'

def test_cors_origins_parsing():
    s = Settings(CORS_ORIGINS='http://localhost:3000, https://ametheus.vercel.app, https://preview-123.vercel.app')
    assert 'http://localhost:3000' in s.CORS_ORIGINS
    assert 'https://ametheus.vercel.app' in s.CORS_ORIGINS
    assert 'https://preview-123.vercel.app' in s.CORS_ORIGINS
