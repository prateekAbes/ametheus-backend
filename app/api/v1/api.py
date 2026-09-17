from fastapi import APIRouter
from app.api.v1.endpoints import games, teams, tournaments, matches, leaderboard, admin, auth, organizer, payments, notifications

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication & Users"])
api_router.include_router(games.router, prefix="/games", tags=["Games"])
api_router.include_router(teams.router, prefix="/teams", tags=["Teams"])
api_router.include_router(tournaments.router, prefix="/tournaments", tags=["Tournaments"])
api_router.include_router(organizer.router, prefix="/organizer", tags=["Organizer Management"])
api_router.include_router(matches.router, prefix="/matches", tags=["Matches"])
api_router.include_router(leaderboard.router, prefix="/leaderboard", tags=["Leaderboard"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin Operations"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments & Escrow"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])

