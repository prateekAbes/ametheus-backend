from fastapi import APIRouter
from app.api.v1.endpoints import (
    games,
    teams,
    admin,
    auth,
    payments,
    upi_accounts,
    wallet,
    notifications,
    media,
    leaderboard,
    organizer,
    tournaments,
    matches,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication & Users"])
api_router.include_router(games.router, prefix="/games", tags=["Games"])
api_router.include_router(teams.router, prefix="/teams", tags=["Teams"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments"])
api_router.include_router(upi_accounts.router, prefix="/admin/upi-accounts", tags=["Admin UPI Accounts"])
api_router.include_router(wallet.router, prefix="/wallet", tags=["Wallet & Withdrawals"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin Operations"])
api_router.include_router(leaderboard.router, prefix="/leaderboard", tags=["Leaderboard"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
api_router.include_router(media.router, prefix="/media", tags=["Media & Image Uploads"])
api_router.include_router(organizer.router, prefix="/organizer", tags=["Tournament Organizer"])
api_router.include_router(tournaments.router, prefix="/tournaments", tags=["Tournaments"])
api_router.include_router(matches.router, prefix="/matches", tags=["Matches"])
