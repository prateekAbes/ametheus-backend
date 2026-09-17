from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import engine, Base
from app.api.v1.api import api_router
from app.models import *  # ensure all models registered

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables and reconcile schema on startup (SQLite and PostgreSQL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            def _reconcile_schema(sync_conn):
                from sqlalchemy import inspect, text
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())

                if "tournaments" in tables:
                    cols = {c['name'] for c in insp.get_columns("tournaments")}
                    if "organizer_id" not in cols:
                        sync_conn.execute(text("ALTER TABLE tournaments ADD COLUMN organizer_id VARCHAR(64)"))

                if "audit_logs" in tables:
                    cols = {c['name'] for c in insp.get_columns("audit_logs")}
                    if "prev_hash" not in cols:
                        sync_conn.execute(text("ALTER TABLE audit_logs ADD COLUMN prev_hash VARCHAR(64)"))
                    if "record_hash" not in cols:
                        sync_conn.execute(text("ALTER TABLE audit_logs ADD COLUMN record_hash VARCHAR(64)"))

                if "tournament_registrations" in tables:
                    cols = {c['name'] for c in insp.get_columns("tournament_registrations")}
                    if "order_id" not in cols:
                        sync_conn.execute(text("ALTER TABLE tournament_registrations ADD COLUMN order_id VARCHAR(64)"))
                    if "payment_status" not in cols:
                        sync_conn.execute(text("ALTER TABLE tournament_registrations ADD COLUMN payment_status VARCHAR(32) DEFAULT 'FREE'"))
                    if "check_in_status" not in cols:
                        sync_conn.execute(text("ALTER TABLE tournament_registrations ADD COLUMN check_in_status VARCHAR(32) DEFAULT 'PENDING'"))

                if "users" in tables:
                    cols = {c['name'] for c in insp.get_columns("users")}
                    if "clerk_user_id" not in cols:
                        sync_conn.execute(text("ALTER TABLE users ADD COLUMN clerk_user_id VARCHAR(128)"))
                    if "role" not in cols:
                        sync_conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(32) DEFAULT 'PLAYER'"))
                    if "inr_balance" not in cols:
                        sync_conn.execute(text("ALTER TABLE users ADD COLUMN inr_balance VARCHAR(32) DEFAULT '0'"))
                    if "riot_id" not in cols:
                        sync_conn.execute(text("ALTER TABLE users ADD COLUMN riot_id VARCHAR(64)"))
                    if "steam_id" not in cols:
                        sync_conn.execute(text("ALTER TABLE users ADD COLUMN steam_id VARCHAR(64)"))

            await conn.run_sync(_reconcile_schema)
        
        # Auto-seed database if empty
        from app.seed import seed_if_empty
        await seed_if_empty()
    except Exception as e:
        print(f"[AMETHEUS Lifespan Warning]: {e}")
    
    yield
    await engine.dispose()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

cors_origins = settings.CORS_ORIGINS if isinstance(settings.CORS_ORIGINS, list) else [settings.CORS_ORIGINS]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi import Request
from fastapi.responses import JSONResponse
import traceback

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_trace = traceback.format_exc()
    print(f"[AMETHEUS Error]: {error_trace}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc),
            "type": type(exc).__name__,
        },
    )

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
async def root():
    return {
        "engine": "AMETHEUS Tournament Engine",
        "version": "1.0.0",
        "status": "operational",
        "docs": "/docs",
    }

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "ametheus-backend"}

@app.get("/health/db")
async def db_health():
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import select, func
    from app.models.game import Game
    from app.models.tournament import Tournament
    from app.models.team import Team
    from app.models.user import User
    from app.models.match import Match
    try:
        async with AsyncSessionLocal() as session:
            g = (await session.execute(select(func.count(Game.id)))).scalar() or 0
            t = (await session.execute(select(func.count(Tournament.id)))).scalar() or 0
            tm = (await session.execute(select(func.count(Team.id)))).scalar() or 0
            u = (await session.execute(select(func.count(User.id)))).scalar() or 0
            m = (await session.execute(select(func.count(Match.id)))).scalar() or 0
            return {
                "status": "connected",
                "counts": {
                    "games": g,
                    "tournaments": t,
                    "teams": tm,
                    "users": u,
                    "matches": m,
                }
            }
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "trace": traceback.format_exc()}

@app.post("/health/seed")
async def trigger_seed():
    from app.seed import populate_seed_data
    from app.core.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            await populate_seed_data(session)
            return {"status": "success", "message": "Database seeded successfully"}
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "trace": traceback.format_exc()}


from fastapi import WebSocket, WebSocketDisconnect
from app.core.ws import ws_manager

@app.websocket("/ws/ticker")
async def websocket_match_ticker(websocket: WebSocket):
    """
    Real-time live match ticker WebSocket broadcast.
    """
    await ws_manager.connect(websocket)
    try:
        # Initial handshake message
        await websocket.send_json({
            "type": "CONNECTION_ESTABLISHED",
            "message": "Connected to AMETHEUS Live Match Engine Ticker",
        })
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)
