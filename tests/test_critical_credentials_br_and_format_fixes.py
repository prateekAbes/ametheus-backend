import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.models.tournament import Tournament
from app.models.match import Match
from app.api.deps import get_current_user, get_current_user_optional, require_super_admin, require_organizer


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db


@pytest.fixture
def make_client(mock_db):
    from fastapi import Header, Depends, HTTPException
    user_map = {}
    app.dependency_overrides[get_db] = lambda: mock_db

    async def _dynamic_current_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = authorization.replace("Bearer ", "").strip()
        if token in user_map:
            return user_map[token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _dynamic_optional_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization.replace("Bearer ", "").strip()
        return user_map.get(token)

    async def _dynamic_require_organizer(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["ORGANIZER", "SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden")

    async def _dynamic_require_super_admin(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden")

    app.dependency_overrides[get_current_user] = _dynamic_current_user
    app.dependency_overrides[get_current_user_optional] = _dynamic_optional_user
    app.dependency_overrides[require_organizer] = _dynamic_require_organizer
    app.dependency_overrides[require_super_admin] = _dynamic_require_super_admin

    def _create(user: User | None = None):
        headers = {}
        if user:
            user_map[user.id] = user
            headers["Authorization"] = f"Bearer {user.id}"
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test", headers=headers)
    return _create


@pytest.mark.asyncio
async def test_match_credentials_save_reopen_and_access_control(mock_db, make_client):
    """
    Verifies the complete match credentials lifecycle:
    1. Organizer saves credentials (unreleased).
    2. Organizer re-fetches match details and match list -> credentials persist and are visible to organizer.
    3. Public/unregistered visitor calls match endpoints -> private credentials are strictly stripped (None).
    4. Organizer releases credentials -> check-in opens, credentials_released becomes True.
    5. Registered participant fetches match details -> released credentials are fully visible.
    6. Unregistered visitor still cannot see credentials.
    """
    admin = User(
        id="org_user_1",
        clerk_user_id="clerk_org_1",
        email="organizer@ametheus.gg",
        username="top_organizer",
        role="ORGANIZER",
    )
    participant = User(
        id="player_reg_1",
        clerk_user_id="clerk_player_1",
        email="player@ametheus.gg",
        username="pro_gamer",
        role="USER",
    )
    unregistered = User(
        id="unreg_user_1",
        clerk_user_id="clerk_unreg_1",
        email="random@ametheus.gg",
        username="casual_viewer",
        role="USER",
    )
    await mock_db.users.insert_one(admin.to_mongo())
    await mock_db.users.insert_one(participant.to_mongo())
    await mock_db.users.insert_one(unregistered.to_mongo())

    tourn_id = "tourn_cred_test_01"
    now_iso = datetime.now(timezone.utc).isoformat()
    tourn = Tournament(
        id=tourn_id,
        slug="cred-test-cup",
        title="Credentials Cup",
        game_id="valorant",
        format="Knockout",
        status="REGISTRATION_CLOSED",
        starts_at=now_iso,
        roster_lock_time=now_iso,
        whatsapp_group_url="https://chat.whatsapp.com/test_room_wa",
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    match_id = "match_cred_01"
    match = Match(
        id=match_id,
        tournament_id=tourn_id,
        stage="Semifinals",
        round_name="Semifinals",
        team_a_id="player_reg_1",
        team_a_name="Pro Gamer",
        team_b_id="team_b_99",
        team_b_name="Rival Squad",
        status="SCHEDULED",
        server_region="AWS Mumbai (ap-south-1)",
    )
    await mock_db.matches.insert_one(match.to_mongo())

    # Participant registration
    await mock_db.tournament_registrations.insert_one({
        "id": "reg_01",
        "tournament_id": tourn_id,
        "player_id": "player_reg_1",
        "team_name": "Pro Gamer",
        "payment_status": "PAID",
        "status": "CONFIRMED",
    })

    admin_client = make_client(admin)
    part_client = make_client(participant)
    unreg_client = make_client(unregistered)
    public_client = make_client(None)

    # 1. Organizer saves credentials via /admin/matches/{id}/credentials
    save_res = await admin_client.put(
        f"/api/v1/admin/matches/{match_id}/credentials",
        json={
            "room_id": "AMT-VALO-ROOM-88",
            "room_password": "SECRET_PIN_442",
            "custom_instructions": "Join slot 1-5, map Ascent",
        },
    )
    assert save_res.status_code == 200
    save_data = save_res.json()
    assert save_data["status"] == "success"
    assert save_data["credentials_released"] is False

    # 2. Organizer reopens match via /matches/{id} -> credentials persist and are visible!
    org_get = await admin_client.get(f"/api/v1/matches/{match_id}")
    assert org_get.status_code == 200
    org_m = org_get.json()
    assert org_m["room_id"] == "AMT-VALO-ROOM-88"
    assert org_m["room_password"] == "SECRET_PIN_442"
    assert org_m["custom_instructions"] == "Join slot 1-5, map Ascent"
    assert org_m["credentials_released"] is False

    # Organizer reopens tournament match list via /matches -> credentials visible in list!
    org_list = await admin_client.get(f"/api/v1/matches?tournament_id={tourn_id}")
    assert org_list.status_code == 200
    matches_list = org_list.json()
    assert len(matches_list) == 1
    assert matches_list[0]["room_id"] == "AMT-VALO-ROOM-88"
    assert matches_list[0]["room_password"] == "SECRET_PIN_442"
    assert matches_list[0]["credentials_released"] is False

    # 3. Before release: Public visitor and registered participant MUST NOT see credentials
    pub_get = await public_client.get(f"/api/v1/matches/{match_id}")
    assert pub_get.status_code == 200
    assert pub_get.json()["room_id"] is None
    assert pub_get.json()["room_password"] is None

    part_get_before = await part_client.get(f"/api/v1/matches/{match_id}")
    assert part_get_before.status_code == 200
    assert part_get_before.json()["room_id"] is None
    assert part_get_before.json()["room_password"] is None

    # 4. Organizer releases credentials
    rel_res = await admin_client.post(f"/api/v1/admin/matches/{match_id}/release-credentials")
    assert rel_res.status_code == 200
    rel_data = rel_res.json()
    assert rel_data["credentials_released"] is True
    assert rel_data.get("status") in ["success", "CHECK_IN_OPEN"]

    # 5. Eligible registered participant reopens match -> credentials now visible!
    part_get_after = await part_client.get(f"/api/v1/matches/{match_id}")
    assert part_get_after.status_code == 200
    part_m = part_get_after.json()
    assert part_m["room_id"] == "AMT-VALO-ROOM-88"
    assert part_m["room_password"] == "SECRET_PIN_442"
    assert part_m["custom_instructions"] == "Join slot 1-5, map Ascent"
    assert part_m["credentials_released"] is True

    # Join info endpoint also returns credentials for eligible participant
    join_info_res = await part_client.get(f"/api/v1/matches/{match_id}/join-info")
    assert join_info_res.status_code == 200
    assert join_info_res.json()["room_id"] == "AMT-VALO-ROOM-88"
    assert join_info_res.json()["room_password"] == "SECRET_PIN_442"

    # 6. Unregistered user still CANNOT see room credentials even after release!
    unreg_get = await unreg_client.get(f"/api/v1/matches/{match_id}")
    assert unreg_get.status_code == 200
    assert unreg_get.json()["room_id"] is None
    assert unreg_get.json()["room_password"] is None


@pytest.mark.asyncio
async def test_admin_update_match_details_credentials_persistence(mock_db, make_client):
    """
    Verifies that updating a match via /admin/matches/{id}/update persists lobby credentials and region.
    """
    admin = User(
        id="admin_upd_1",
        clerk_user_id="clerk_upd_1",
        email="admin@ametheus.gg",
        username="super_updater",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    match_id = "match_upd_cred_99"
    match = Match(
        id=match_id,
        tournament_id="tourn_upd_99",
        status="SCHEDULED",
        server_region="AWS Mumbai (ap-south-1)",
    )
    await mock_db.matches.insert_one(match.to_mongo())

    admin_client = make_client(admin)
    res = await admin_client.patch(
        f"/api/v1/admin/matches/{match_id}/update",
        json={
            "lobby_code": "NEW_LOBBY_ROOM_777",
            "lobby_password": "NEW_PASSWORD_999",
            "server_region": "AWS Singapore (ap-southeast-1)",
            "scheduled_time": "08:30 PM IST",
        },
    )
    assert res.status_code == 200

    saved_doc = await mock_db.matches.find_one({"$or": [{"_id": match_id}, {"id": match_id}]})
    assert saved_doc is not None
    assert saved_doc["room_id"] == "NEW_LOBBY_ROOM_777"
    assert saved_doc["lobby_code"] == "NEW_LOBBY_ROOM_777"
    assert saved_doc["room_password"] == "NEW_PASSWORD_999"
    assert saved_doc["server_region"] == "AWS Singapore (ap-southeast-1)"
    assert saved_doc["scheduled_time"] == "08:30 PM IST"


@pytest.mark.asyncio
async def test_battle_royale_25_teams_no_team_a_b(mock_db, make_client):
    """
    Verifies that a Battle Royale match containing 25 registered teams:
    1. Contains all 25 teams in its participant roster.
    2. Does NOT generate fake Team A vs Team B placeholders or TBD.
    """
    admin = User(
        id="admin_br_25",
        clerk_user_id="clerk_br_25",
        email="admin@ametheus.gg",
        username="br_admin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    tourn_id = "br_tourn_25_teams"
    now_iso = datetime.now(timezone.utc).isoformat()
    tourn = Tournament(
        id=tourn_id,
        slug="br-tourn-25",
        title="25 Squads Apex Clash",
        game_id="apex-legends",
        format="Battle Royale",
        status="REGISTRATION_CLOSED",
        registered_teams=25,
        max_teams=25,
        starts_at=now_iso,
        roster_lock_time=now_iso,
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    # Insert 25 registrations
    for i in range(1, 26):
        await mock_db.tournament_registrations.insert_one({
            "id": f"reg_{i}",
            "tournament_id": tourn_id,
            "team_id": f"team_{i}",
            "team_name": f"Elite Squad {i}",
            "payment_status": "PAID",
            "status": "CONFIRMED",
        })

    admin_client = make_client(admin)
    gen_res = await admin_client.post(
        f"/api/v1/admin/tournaments/{tourn_id}/generate-battle-royale-matches?round_number=1&lobby_size=25"
    )
    assert gen_res.status_code == 200

    match_doc = await mock_db.matches.find_one({"tournament_id": tourn_id})
    assert match_doc is not None
    assert match_doc.get("team_a_id") is None
    assert match_doc.get("team_b_id") is None
    assert match_doc.get("team_a_name") is None
    assert match_doc.get("team_b_name") is None
    assert len(match_doc.get("team_ids", [])) == 25
    assert len(match_doc.get("teams", [])) == 25


@pytest.mark.asyncio
async def test_community_whatsapp_link_available_upon_registration(mock_db, make_client):
    """
    Verifies that once a user registers and is confirmed (or pending verification),
    they can immediately access the tournament's official WhatsApp group link via
    /tournaments/{id}/community even while registration is still open.
    """
    player = User(
        id="player_wa_01",
        clerk_user_id="clerk_wa_01",
        email="wa_player@ametheus.gg",
        username="wa_contender",
        role="USER",
    )
    await mock_db.users.insert_one(player.to_mongo())

    tourn_id = "wa_tourn_test_99"
    now_iso = datetime.now(timezone.utc).isoformat()
    tourn = Tournament(
        id=tourn_id,
        slug="wa-cup",
        title="WhatsApp Cup",
        game_id="bgmi",
        format="Battle Royale",
        status="REGISTRATION_OPEN",  # Registration is STILL OPEN!
        starts_at=now_iso,
        roster_lock_time=now_iso,
        whatsapp_group_url="https://chat.whatsapp.com/official_wa_invite_link",
        discord_url="https://discord.gg/official_discord",
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    player_client = make_client(player)

    # 1. Before registration: 403 Forbidden
    res_before = await player_client.get(f"/api/v1/tournaments/{tourn_id}/community")
    assert res_before.status_code == 403

    # 2. Player registers (e.g. Free entry or Paid with wallet)
    await mock_db.tournament_registrations.insert_one({
        "id": "reg_wa_player_01",
        "tournament_id": tourn_id,
        "player_id": "player_wa_01",
        "team_name": "WA Contender",
        "payment_status": "PAID",
        "status": "CONFIRMED",
    })

    # 3. Immediately after registration, player can fetch community link!
    res_after = await player_client.get(f"/api/v1/tournaments/{tourn_id}/community")
    assert res_after.status_code == 200
    data = res_after.json()
    assert data["whatsapp_group_url"] == "https://chat.whatsapp.com/official_wa_invite_link"
    assert data["discord_url"] == "https://discord.gg/official_discord"
