from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.match import Match
from app.services.notification import create_notification
from app.core.ws import ws_manager

REMINDER_TITLES = {
    "ONE_WEEK": "Your tournament match is in 1 week",
    "TWO_DAYS": "Your tournament match is in 2 days",
    "ONE_DAY": "Your tournament match is tomorrow",
    "FIFTEEN_MINUTES": "Your match starts in 15 minutes",
    "TEN_MINUTES": "Your match starts in 10 minutes",
    "STARTING_NOW": "Your match is starting now",
    "STARTED": "Your match has started",
}

REMINDER_MESSAGES = {
    "ONE_WEEK": "Your tournament match is scheduled for {time}. Keep an eye on your notifications for further updates.",
    "TWO_DAYS": "Your tournament match starts in 2 days at {time}. Ensure your roster is ready.",
    "ONE_DAY": "Your match starts tomorrow on {time}. Make sure you are ready and check the tournament information before the match.",
    "FIFTEEN_MINUTES": "Your match is about to begin. Open the match page to view the latest match information and join details.",
    "TEN_MINUTES": "Your match starts in 10 minutes. Get ready and check your match details.",
    "STARTING_NOW": "Your match is now scheduled to begin. Check the match page for the latest information.",
    "STARTED": "Your match is currently live. Enter the game room now.",
}

async def get_eligible_match_participant_ids(
    match: Match,
    db: AsyncIOMotorDatabase
) -> List[str]:
    """
    Returns the user IDs of all confirmed, paid participants eligible for this match.
    Security: Only participants who have confirmed/free registration and belong to
    either Team A or Team B are returned.
    """
    tournament_id = match.tournament_id
    tourn_query = [tournament_id]
    tourn = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
    if tourn:
        tourn_query.extend([str(tourn.get("_id")), tourn.get("id"), tourn.get("slug")])
    tourn_query = list(set(q for q in tourn_query if q))

    team_identifiers = [tid for tid in [match.team_a_id, match.team_b_id] if tid]
    team_names = [name for name in [match.team_a_name, match.team_b_name, match.team_a_placeholder, match.team_b_placeholder] if name]
    participant_user_ids = set()

    # 1. Check teams by ID or name
    teams_query = []
    if team_identifiers:
        teams_query.extend([{"_id": {"$in": team_identifiers}}, {"id": {"$in": team_identifiers}}])
    if team_names:
        teams_query.append({"name": {"$in": team_names}})

    if teams_query:
        teams_cursor = db.teams.find({"$or": teams_query})
        async for team in teams_cursor:
            t_id_str = str(team.get("_id", team.get("id")))
            t_name = team.get("name")
            reg = await db.tournament_registrations.find_one({
                "tournament_id": {"$in": tourn_query},
                "$or": [
                    {"team_id": {"$in": [t_id_str, team.get("id")]}},
                    {"team_name": t_name},
                    {"captain_id": team.get("captain_id")},
                ],
                "payment_status": {"$in": ["PAID", "FREE", "APPROVED"]},
            })
            if reg:
                if team.get("captain_id"):
                    participant_user_ids.add(team["captain_id"])
                for member in team.get("members", []):
                    uid = member.get("user_id") if isinstance(member, dict) else getattr(member, "user_id", None)
                    if uid:
                        participant_user_ids.add(uid)

    # 2. Contender might be a solo player
    users_query = []
    if team_identifiers:
        users_query.extend([{"_id": {"$in": team_identifiers}}, {"id": {"$in": team_identifiers}}])
    if team_names:
        users_query.append({"username": {"$in": team_names}})

    if users_query:
        users_cursor = db.users.find({"$or": users_query})
        async for user in users_cursor:
            u_id = str(user.get("_id", user.get("id")))
            reg = await db.tournament_registrations.find_one({
                "tournament_id": {"$in": tourn_query},
                "$or": [
                    {"player_id": u_id},
                    {"captain_id": u_id},
                ],
                "payment_status": {"$in": ["PAID", "FREE", "APPROVED"]},
            })
            if reg:
                participant_user_ids.add(u_id)

    return list(participant_user_ids)

async def send_match_reminder(
    match_id: str,
    reminder_type: str,
    db: AsyncIOMotorDatabase,
    custom_message: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Sends a match countdown reminder to all confirmed, eligible participants of the match.
    Enforces deduplication: will not send the same reminder twice unless forced.
    """
    match_doc = await db.matches.find_one({"$or": [{"_id": match_id}, {"id": match_id}]})
    if not match_doc:
        return {"status": "error", "message": "Match not found", "sent_count": 0}

    match = Match.from_mongo(match_doc)
    sent_reminders = set(match.sent_reminders or [])

    if reminder_type in sent_reminders and not force:
        return {
            "status": "skipped",
            "message": f"Reminder '{reminder_type}' has already been delivered for this match.",
            "sent_count": 0,
        }

    participants = await get_eligible_match_participant_ids(match, db)
    if not participants:
        return {
            "status": "warning",
            "message": "No confirmed participants found for this match yet.",
            "sent_count": 0,
        }

    # Fetch tournament title
    tourn = await db.tournaments.find_one({"$or": [{"_id": match.tournament_id}, {"id": match.tournament_id}, {"slug": match.tournament_id}]})
    tourn_title = tourn.get("title", "Tournament") if tourn else "Tournament"

    time_str = match.scheduled_time or "Scheduled Time"
    if match.scheduled_at:
        try:
            if isinstance(match.scheduled_at, datetime):
                time_str = match.scheduled_at.strftime("%b %d, %Y at %I:%M %p UTC")
            else:
                time_str = str(match.scheduled_at)
        except Exception:
            pass

    title = REMINDER_TITLES.get(reminder_type, "Match Notification")
    base_msg = custom_message or REMINDER_MESSAGES.get(reminder_type, "Your match is scheduled for {time}.").format(time=time_str)

    # Attach match join credentials if configured and appropriate (Rule 16)
    extra_details = []
    if match.join_info_enabled and match.join_info:
        formatted_fields = []
        for k, v in match.join_info.items():
            if v and str(v).strip():
                label = k.replace("_", " ").title()
                formatted_fields.append(f"{label}: {v}")
        if formatted_fields:
            extra_details.append("Join Details: " + " | ".join(formatted_fields))

    if match.match_message:
        extra_details.append(f"Instructions: {match.match_message}")

    full_message = f"{tourn_title} • {match.stage or match.round_name or 'Match'}\n{base_msg}"
    if extra_details:
        full_message += "\n" + "\n".join(extra_details)

    sent_count = 0
    for uid in participants:
        await create_notification(
            db=db,
            user_id=uid,
            title=title,
            message=full_message,
            notification_type="MATCH",
            link=f"/matches?match_id={match.id}",
            metadata_json=f'{{"tournament_id": "{match.tournament_id}", "match_id": "{match.id}", "reminder_type": "{reminder_type}"}}',
        )
        sent_count += 1

    # Broadcast via WebSocket
    await ws_manager.broadcast({
        "type": "MATCH_REMINDER",
        "match_id": match.id,
        "tournament_id": match.tournament_id,
        "reminder_type": reminder_type,
        "title": title,
        "message": base_msg,
    })

    # Record deduplication in database
    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$addToSet": {"sent_reminders": reminder_type}}
    )

    return {
        "status": "success",
        "message": f"Successfully sent '{reminder_type}' reminder to {sent_count} match participants.",
        "sent_count": sent_count,
        "reminder_type": reminder_type,
    }

async def invalidate_reminders_on_schedule_change(
    match_id: str,
    new_time: str,
    new_datetime: Optional[datetime],
    db: AsyncIOMotorDatabase
) -> None:
    """
    When match schedule changes (Rule 19):
    1. Notifies participants of the updated schedule.
    2. Invalidates previously sent countdown reminders so future reminders are recalculated.
    """
    match_doc = await db.matches.find_one({"$or": [{"_id": match_id}, {"id": match_id}]})
    if not match_doc:
        return

    match = Match.from_mongo(match_doc)
    participants = await get_eligible_match_participant_ids(match, db)

    # Reset future countdown reminders, retaining only STARTED if already completed
    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"sent_reminders": []}}
    )

    if participants:
        title = "Match Schedule Updated"
        message = f"Your match ({match.stage or match.round_name or 'Match'}) has been rescheduled to: {new_time}. Please adjust your preparation."
        for uid in participants:
            await create_notification(
                db=db,
                user_id=uid,
                title=title,
                message=message,
                notification_type="MATCH",
                link=f"/matches?match_id={match.id}",
                metadata_json=f'{{"tournament_id": "{match.tournament_id}", "match_id": "{match.id}", "action": "RESCHEDULED"}}',
            )

        await ws_manager.broadcast({
            "type": "MATCH_RESCHEDULED",
            "match_id": match.id,
            "tournament_id": match.tournament_id,
            "new_time": new_time,
        })

async def notify_join_info_updated(
    match_id: str,
    db: AsyncIOMotorDatabase
) -> None:
    """
    Notifies participants when the organizer updates match join information (Rule 17).
    """
    match_doc = await db.matches.find_one({"$or": [{"_id": match_id}, {"id": match_id}]})
    if not match_doc:
        return

    match = Match.from_mongo(match_doc)
    participants = await get_eligible_match_participant_ids(match, db)

    if participants:
        title = "Match Join Details Updated"
        message = f"New lobby join credentials and instructions have been posted for your match in {match.stage or match.round_name or 'Match'}. Check your match page to enter the room."
        for uid in participants:
            await create_notification(
                db=db,
                user_id=uid,
                title=title,
                message=message,
                notification_type="MATCH",
                link=f"/matches?match_id={match.id}",
                metadata_json=f'{{"tournament_id": "{match.tournament_id}", "match_id": "{match.id}", "action": "JOIN_INFO_UPDATED"}}',
            )
