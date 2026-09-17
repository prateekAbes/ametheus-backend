import time
import traceback
from collections import defaultdict
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import get_database, init_db_indexes
from app.api.v1.api import api_router
from app.models import *

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-initialize MongoDB indexes and seed if empty
    try:
        db = get_database()
        await init_db_indexes(db)
        
        # Auto-seed database if empty
        from app.seed import seed_if_empty
        await seed_if_empty()
    except Exception as e:
        print(f"[AMETHEUS Lifespan Warning]: {e}")
    
    yield

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# CORS Configuration
configured_origins = list(settings.CORS_ORIGINS) if isinstance(settings.CORS_ORIGINS, list) else [settings.CORS_ORIGINS]
for origin in ["https://ametheus-frontend.vercel.app", "http://localhost:3000", "http://127.0.0.1:3000"]:
    if origin not in configured_origins:
        configured_origins.append(origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$|^https://.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate Limiting & Request Logging Middleware
RATE_LIMIT_WINDOW = 60.0  # seconds
RATE_LIMIT_MAX_REQUESTS = 120
_ip_request_timestamps: dict[str, list[float]] = defaultdict(list)

EXEMPT_PATHS = {
    "/",
    "/health",
    "/health/db",
    "/health/seed",
    "/docs",
    "/redoc",
    "/openapi.json",
    f"{settings.API_V1_STR}/openapi.json",
}

@app.middleware("http")
async def rate_limit_and_logging_middleware(request: Request, call_next):
    path = request.url.path

    # Rate Limiting Check (exempt health, docs, openapi, and WebSocket paths)
    if path not in EXEMPT_PATHS and not path.startswith("/ws/"):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        timestamps = [ts for ts in _ip_request_timestamps[client_ip] if now - ts < RATE_LIMIT_WINDOW]
        if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Rate limit exceeded (120 req/min). Please try again shortly."
                },
            )
        timestamps.append(now)
        _ip_request_timestamps[client_ip] = timestamps

    # Request Logging
    start_time = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000
    print(f"[AMETHEUS HTTP] {request.method} {path} -> {response.status_code} ({duration_ms:.1f}ms)")
    return response

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
        "database": "MongoDB",
        "status": "operational",
        "docs": "/docs",
    }

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "ametheus-backend", "database": "mongodb"}

@app.get("/health/db")
async def db_health():
    try:
        db = get_database()
        await db.command("ping")
        u_count = await db.users.count_documents({})
        g_count = await db.games.count_documents({})
        t_count = await db.tournaments.count_documents({})
        tm_count = await db.teams.count_documents({})
        m_count = await db.matches.count_documents({})
        return {
            "status": "connected",
            "database": "mongodb",
            "counts": {
                "users": u_count,
                "games": g_count,
                "tournaments": t_count,
                "teams": tm_count,
                "matches": m_count,
            }
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "trace": traceback.format_exc()}

@app.post("/health/seed")
async def trigger_seed():
    from app.seed import populate_seed_data
    try:
        db = get_database()
        await init_db_indexes(db)
        await populate_seed_data(db)
        return {"status": "success", "message": "MongoDB seeded successfully"}
    except Exception as e:
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
