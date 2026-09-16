from fastapi import APIRouter
from app.api.v1.endpoints import games, teams, tournaments, matches, leaderboard, admin

api_router = APIRouter()
api_router.include_router(games.router, prefix="/games", tags=["Games"])
api_router.include_router(teams.router, prefix="/teams", tags=["Teams"])
api_router.include_router(tournaments.router, prefix="/tournaments", tags=["Tournaments"])
api_router.include_router(matches.router, prefix="/matches", tags=["Matches"])
api_router.include_router(leaderboard.router, prefix="/leaderboard", tags=["Leaderboard"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin Operations"])
