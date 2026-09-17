import asyncio
import json
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_database, init_db_indexes
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.tournament import Tournament, TournamentRegistration
from app.models.match import Match
from app.models.admin import TournamentAdminAssignment
from app.services.audit import record_audit_log

GAMES_SEED = [
    {
        "slug": "valorant",
        "name": "Valorant",
        "category": "pc",
        "platform": "PC (Riot Vanguard)",
        "tag": "5V5 TACTICAL PC",
        "is_team_game": True,
        "min_team_size": 5,
        "max_team_size": 5,
        "banner_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuD-g0bK72p2tQe7b3Z3F5WzX8oV0h4vM1bX2m9x_banner",
        "logo_url": "https://lh3.googleusercontent.com/aida/AEtjO1VmHtH5AesKwgS_TvDCwn_SByK0_Y2-LCXofj-hkKIbHqWcjtYeWtK2_EEl0U6I_DmJoPM-hMeShLdy_nCTwqrupd0unrD25aXsSvvI0OfJBxOaEzlaOACJ3bDytrajhca2jxZqtPhhO3DoFVV3rxuRhkvK_K9IraGcUJRRWSQxGOnhw9qH30bwm6frEhsnAeMqCJzw5qD6MOSPJgqqxiGtfGcSAur7l9TiMeGeCUnzHpMZAuWd08ClP4w",
        "description": "5v5 tactical hero-shooter by Riot Games. Precise gunplay meets adaptable agent abilities in verified tournament matches.",
        "server_region": "AWS Mumbai (ap-south-1)",
        "anti_cheat": "Hardware TPM 2.0 Vanguard",
        "match_protocol": "Competitive MR12",
        "accent_color": "#E56A10",
    },
    {
        "slug": "counter-strike-2",
        "name": "Counter-Strike 2",
        "category": "pc",
        "platform": "PC (Steam)",
        "tag": "5V5 PC",
        "is_team_game": True,
        "min_team_size": 5,
        "max_team_size": 5,
        "banner_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuCS2_Banner",
        "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuCS2_Logo",
        "description": "The pinnacle of tactical competitive esports. Valve Anti-Cheat protected matches with 128-tick subtick rate.",
        "server_region": "Valve India Official (Mumbai)",
        "anti_cheat": "VAC Live + Server Telemetry",
        "match_protocol": "Competitive MR12",
        "accent_color": "#F59E0B",
    },
    {
        "slug": "bgmi",
        "name": "BGMI",
        "category": "mobile",
        "platform": "Mobile (Android / iOS)",
        "tag": "BATTLE ROYALE MOBILE",
        "is_team_game": True,
        "min_team_size": 4,
        "max_team_size": 4,
        "banner_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuBGMI_Banner",
        "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuBGMI_Logo",
        "description": "Battlegrounds Mobile India squad competitions across Erangel and Miramar. Custom tournament rooms with anti-spectator delay.",
        "server_region": "Krafton India Direct",
        "anti_cheat": "Security System v3.2",
        "match_protocol": "16 Squads Points Table",
        "accent_color": "#E56A10",
    },
    {
        "slug": "chess",
        "name": "Chess",
        "category": "solo",
        "platform": "Web / Mobile",
        "tag": "1V1 BLITZ & RAPID",
        "is_team_game": False,
        "min_team_size": 1,
        "max_team_size": 1,
        "banner_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuChess_Banner",
        "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuChess_Logo",
        "description": "Classical minds in modern blitz and rapid competition. Certified FIDE / Chess.com fair play screening.",
        "server_region": "Global Arbiter Node",
        "anti_cheat": "Stockfish 16 Engine Cross-Audit",
        "match_protocol": "3+2 Blitz / 10+0 Rapid",
        "accent_color": "#3B82F6",
    },
    {
        "slug": "smash-karts",
        "name": "Smash Karts",
        "category": "crossplay",
        "platform": "Browser / WebGL",
        "tag": "3D BATTLE ARENA",
        "is_team_game": True,
        "min_team_size": 3,
        "max_team_size": 3,
        "banner_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuSmash_Banner",
        "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuSmash_Logo",
        "description": "High-octane 3D kart battle arena. Collect weapons, eliminate rivals, and control point zones in custom private rooms.",
        "server_region": "Asia-Pacific Edge (Singapore)",
        "anti_cheat": "Server-Authoritative Physics",
        "match_protocol": "Best of 5 Battle Rounds",
        "accent_color": "#10B981",
    },
    {
        "slug": "mini-militia",
        "name": "Mini Militia",
        "category": "mobile",
        "platform": "Mobile (Android / iOS)",
        "tag": "2D MULTIPLAYER",
        "is_team_game": True,
        "min_team_size": 4,
        "max_team_size": 4,
        "banner_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuMilitia_Banner",
        "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuMilitia_Logo",
        "description": "Classic 2D multiplayer combat. Custom room setups with outlaw weapons and no-grenade-spam rules.",
        "server_region": "India Central Cloud",
        "anti_cheat": "Client Mod Validation",
        "match_protocol": "10-Minute Outpost Deathmatch",
        "accent_color": "#8B5CF6",
    },
]

async def populate_seed_data(db: AsyncIOMotorDatabase):
    # 1. Users
    seed_users = [
        User(
            id="usr_super_admin",
            clerk_user_id="user_super_admin_clerk",
            email="admin@ametheus.gg",
            username="super_admin",
            display_name="Super Admin",
            role="SUPER_ADMIN",
        ),
        User(
            id="usr_arbiter_val",
            clerk_user_id="user_arbiter_val_clerk",
            email="arbiter@ametheus.gg",
            username="arbiter_val",
            display_name="Valorant Arbiter",
            role="ORGANIZER",
        ),
        User(
            id="usr_vortex_lead",
            clerk_user_id="user_vortex_lead_clerk",
            email="vortex@solaris.gg",
            username="vortex_lead",
            display_name="Vortex Lead",
            role="PLAYER",
            riot_id="Vortex#APEX",
            steam_id="STEAM_0:1:88291039",
        ),
        User(
            id="usr_phantom",
            email="phantom@solaris.gg",
            username="phantom",
            display_name="Phantom",
            role="PLAYER",
        ),
        User(
            id="usr_omen",
            email="omen@solaris.gg",
            username="omen",
            display_name="Omen",
            role="PLAYER",
        ),
        User(
            id="usr_sova",
            email="sova@solaris.gg",
            username="sova",
            display_name="Sova",
            role="PLAYER",
        ),
        User(
            id="usr_jett",
            email="jett@solaris.gg",
            username="jett",
            display_name="Jett",
            role="PLAYER",
        ),
        User(
            id="usr_apex_lead",
            email="lead@apexstrikers.gg",
            username="apex_lead",
            display_name="Apex Leader",
            role="PLAYER",
        ),
    ]

    for u in seed_users:
        await db.users.update_one(
            {"$or": [{"_id": u.id}, {"email": u.email}, {"username": u.username}]},
            {"$setOnInsert": u.to_mongo()},
            upsert=True
        )

    vortex_doc = await db.users.find_one({"username": "vortex_lead"})
    apex_doc = await db.users.find_one({"username": "apex_lead"})
    org_doc = await db.users.find_one({"username": "arbiter_val"})
    admin_doc = await db.users.find_one({"username": "super_admin"})

    vortex_id = str(vortex_doc["_id"]) if vortex_doc else "usr_vortex_lead"
    apex_id = str(apex_doc["_id"]) if apex_doc else "usr_apex_lead"
    org_id = str(org_doc["_id"]) if org_doc else "usr_arbiter_val"
    admin_id = str(admin_doc["_id"]) if admin_doc else "usr_super_admin"

    # 2. Games
    for g_data in GAMES_SEED:
        existing_g = await db.games.find_one({"slug": g_data["slug"]})
        if not existing_g:
            game = Game(**g_data)
            await db.games.insert_one(game.to_mongo())

    val_game = await db.games.find_one({"slug": "valorant"})
    val_game_id = str(val_game["_id"]) if val_game else "valorant"

    # 3. Teams
    solaris_x = await db.teams.find_one({"name": "Solaris X"})
    if not solaris_x:
        members = [
            TeamMember(user_id=vortex_id, role="CAPTAIN", game_handle="Vortex#APEX"),
            TeamMember(user_id="usr_phantom", role="MEMBER", game_handle="Phantom#999"),
            TeamMember(user_id="usr_omen", role="MEMBER", game_handle="Omen#IND"),
            TeamMember(user_id="usr_sova", role="MEMBER", game_handle="Sova#DARTS"),
            TeamMember(user_id="usr_jett", role="MEMBER", game_handle="Jett#WIND"),
        ]
        s_team = Team(
            id="team_solaris_x",
            game_id=val_game_id,
            captain_id=vortex_id,
            name="Solaris X",
            tag="SLX",
            invite_code="SR7X4K",
            max_members=5,
            members=members,
        )
        await db.teams.insert_one(s_team.to_mongo())
        solaris_x = await db.teams.find_one({"_id": "team_solaris_x"})

    apex_strikers = await db.teams.find_one({"name": "Apex Strikers"})
    if not apex_strikers:
        members = [
            TeamMember(user_id=apex_id, role="CAPTAIN", game_handle="Apex#KING"),
        ]
        a_team = Team(
            id="team_apex_strikers",
            game_id=val_game_id,
            captain_id=apex_id,
            name="Apex Strikers",
            tag="APX",
            invite_code="APX999",
            max_members=5,
            members=members,
        )
        await db.teams.insert_one(a_team.to_mongo())
        apex_strikers = await db.teams.find_one({"_id": "team_apex_strikers"})

    solaris_id = str(solaris_x["_id"])
    apex_team_id = str(apex_strikers["_id"])

    # 4. Tournaments
    val_tourn = await db.tournaments.find_one({"slug": "ametheus-valorant-open-01"})
    if not val_tourn:
        t = Tournament(
            id="tourn_val_open_01",
            slug="ametheus-valorant-open-01",
            game_id=val_game_id,
            organizer_id=org_id,
            title="AMETHEUS VALORANT INVITATIONAL 2026",
            prize_pool_inr=50000,
            entry_fee_inr=60,
            status="REGISTRATION_OPEN",
            starts_at="Today, 8:00 PM IST",
            roster_lock_time="7:45 PM IST",
            server_region="AWS Mumbai (ap-south-1)",
            max_teams=16,
            rank_requirement="Platinum III or Higher",
            format="Single Elimination (BO1 / BO3 Finals)",
            banner_url="https://lh3.googleusercontent.com/aida-public/AB6AXuD-g0bK72p2tQe7b3Z3F5WzX8oV0h4vM1bX2m9x_banner",
            badge_color="#E56A10",
            description="Tier-1 tactical competition featuring the best rosters in the sub-continent.",
            rules=["All players must have Vanguard running", "Coaches allowed only in timeouts", "128-tick Mumbai server"],
            schedule=[{"stage": "Check-in", "time": "7:30 PM", "details": "Mandatory check-in"}],
            prizes=[{"rank": "1st Place", "amount": "₹30,000", "share": "60%", "badge": "GOLD"}],
            bracket_rounds=[],
        )
        await db.tournaments.insert_one(t.to_mongo())
        val_tourn = await db.tournaments.find_one({"_id": "tourn_val_open_01"})

    tourn_id = str(val_tourn["_id"])

    # Registrations
    reg1 = TournamentRegistration(
        tournament_id=tourn_id,
        team_id=solaris_id,
        payment_status="PAID",
        order_id="order_rzp_demo_1",
        check_in_status="CHECKED_IN",
    )
    reg2 = TournamentRegistration(
        tournament_id=tourn_id,
        team_id=apex_team_id,
        payment_status="PAID",
        order_id="order_rzp_demo_2",
        check_in_status="CHECKED_IN",
    )
    for r in [reg1, reg2]:
        await db.tournament_registrations.update_one(
            {"tournament_id": r.tournament_id, "team_id": r.team_id},
            {"$setOnInsert": r.to_mongo()},
            upsert=True,
        )

    # Admin assignment
    assign = TournamentAdminAssignment(
        admin_user_id=org_id,
        tournament_id=tourn_id,
        assigned_by=admin_id,
    )
    await db.tournament_admin_assignments.update_one(
        {"admin_user_id": assign.admin_user_id, "tournament_id": assign.tournament_id},
        {"$setOnInsert": assign.to_mongo()},
        upsert=True,
    )

    # Matches
    match1 = Match(
        tournament_id=tourn_id,
        stage="Quarterfinals — Match 1 (BO3)",
        round_number=1,
        match_number=1,
        format="5v5 Competitive Custom (Ascent / Bind / Haven)",
        scheduled_time="Today, 8:30 PM IST",
        status="LIVE",
        team_a_id=solaris_id,
        team_b_id=apex_team_id,
        score_a=11,
        score_b=9,
        server_region="AWS Mumbai (ap-south-1) — 128 Tick",
        lobby_code="AMETHEUS-VAL-QF1-094",
        lobby_password="VANGUARD_ESCROW_99",
    )
    await db.matches.update_one(
        {"tournament_id": match1.tournament_id, "stage": match1.stage},
        {"$setOnInsert": match1.to_mongo()},
        upsert=True,
    )

    # Audit Log
    await record_audit_log(
        db=db,
        operator="super_admin",
        role="SUPER_ADMIN",
        action="INITIALIZE_PLATFORM_SEED",
        target="Database Seed",
        details="Initial platform seeding with official tournament and teams",
    )

async def seed_if_empty():
    try:
        db = get_database()
        g_count = await db.games.count_documents({})
        t_count = await db.tournaments.count_documents({})
        if g_count == 0 or t_count == 0:
            print(f"[AMETHEUS MongoDB] Database has {g_count} games and {t_count} tournaments. Running idempotent seeding...")
            await populate_seed_data(db)
            print("[AMETHEUS MongoDB] Initial catalog & tournaments seeded successfully!")
        else:
            print(f"[AMETHEUS MongoDB] Database contains {g_count} games and {t_count} tournaments.")
    except Exception as e:
        print(f"[AMETHEUS MongoDB] Seed check notice: {e}")

async def seed():
    db = get_database()
    collections = [
        "users", "games", "teams", "tournaments",
        "tournament_registrations", "matches",
        "tournament_admin_assignments", "disputes",
        "notifications", "audit_logs"
    ]
    for col in collections:
        await db[col].drop()
    await init_db_indexes(db)
    await populate_seed_data(db)
    print("Database full MongoDB re-seed completed successfully!")

if __name__ == "__main__":
    asyncio.run(seed())
