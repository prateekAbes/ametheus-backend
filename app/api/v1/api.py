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
    support,
    regulations,
    email_settings,
    genres,
    reviews,
    organizer_communications,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(organizer_communications.organizer_comm_router, prefix="/organizer", tags=["Organizer Communications"])
api_router.include_router(organizer_communications.admin_comm_router, prefix="/admin", tags=["Admin Organizer Communications"])
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication & Users"])
api_router.include_router(games.router, prefix="/games", tags=["Games"])
api_router.include_router(genres.router, prefix="/genres", tags=["Genres"])
api_router.include_router(genres.admin_router, prefix="/admin/genres", tags=["Admin Genres"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["Reviews"])
api_router.include_router(reviews.admin_router, prefix="/admin/reviews", tags=["Admin Reviews"])
api_router.include_router(teams.router, prefix="/teams", tags=["Teams"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments"])
api_router.include_router(upi_accounts.router, prefix="/admin/upi-accounts", tags=["Admin UPI Accounts"])
api_router.include_router(wallet.router, prefix="/wallet", tags=["Wallet & Withdrawals"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin Operations"])
api_router.include_router(email_settings.admin_router, prefix="/admin/email", tags=["Admin Email & SMTP Settings"])
api_router.include_router(email_settings.public_router, prefix="/platform", tags=["Platform Settings"])
api_router.include_router(leaderboard.router, prefix="/leaderboard", tags=["Leaderboard"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
api_router.include_router(media.router, prefix="/media", tags=["Media & Image Uploads"])
api_router.include_router(organizer.router, prefix="/organizer", tags=["Tournament Organizer"])
api_router.include_router(tournaments.router, prefix="/tournaments", tags=["Tournaments"])
api_router.include_router(matches.router, prefix="/matches", tags=["Matches"])
api_router.include_router(support.router, prefix="/support", tags=["Support & Tickets"])
api_router.include_router(regulations.router, prefix="/regulations", tags=["Tournament Regulations & Discipline"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["Inbound Webhooks"])
