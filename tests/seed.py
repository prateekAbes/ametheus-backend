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
from app.models.dispute import Dispute
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
        "banner_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
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
        "banner_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop",
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
        "banner_url": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop",
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
        "banner_url": "https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1586165368502-1bad197a6461?q=80&w=300&auto=format&fit=crop",
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
        "banner_url": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop",
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
        "banner_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "logo_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop",
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
            display_name="Platform Super Admin",
            role="SUPER_ADMIN",
        ),
        User(
            id="usr_pk_super_admin",
            clerk_user_id="user_pk_super_admin",
            email="pk.4081988@gmail.com",
            username="pk_admin",
            display_name="Prateek (Super Admin)",
            role="SUPER_ADMIN",
        ),
        User(
            id="usr_arbiter_val",
            clerk_user_id="user_arbiter_val_clerk",
            email="arbiter@ametheus.gg",
            username="arbiter_val",
            display_name="Tournament Arbiter",
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
            riot_id="Phantom#999",
        ),
        User(
            id="usr_omen",
            email="omen@solaris.gg",
            username="omen",
            display_name="Omen",
            role="PLAYER",
            riot_id="Omen#IND",
        ),
        User(
            id="usr_sova",
            email="sova@solaris.gg",
            username="sova",
            display_name="Sova",
            role="PLAYER",
            riot_id="Sova#DARTS",
        ),
        User(
            id="usr_jett",
            email="jett@solaris.gg",
            username="jett",
            display_name="Jett",
            role="PLAYER",
            riot_id="Jett#WIND",
        ),
        User(
            id="usr_apex_lead",
            email="lead@apexstrikers.gg",
            username="apex_lead",
            display_name="Apex Leader",
            role="PLAYER",
            riot_id="Apex#KING",
        ),
        User(
            id="usr_s1mple",
            email="s1mple@navi.gg",
            username="s1mple_mumbai",
            display_name="s1mple IN",
            role="PLAYER",
            steam_id="STEAM_0:0:19283746",
        ),
        User(
            id="usr_jonathan",
            email="jonathan@godlike.gg",
            username="jonathan_gaming",
            display_name="Jonathan Gaming",
            role="PLAYER",
        ),
        User(
            id="usr_magnus",
            email="magnus@chessclub.gg",
            username="magnus_blitz",
            display_name="Magnus B",
            role="PLAYER",
        ),
    ]

    # Clean up any legacy documents with clerk_user_id: null so sparse index remains valid
    await db.users.update_many({"clerk_user_id": None}, {"$unset": {"clerk_user_id": ""}})

    for u in seed_users:
        u_data = u.to_mongo()
        u_id = u_data.pop("_id")
        if u_data.get("clerk_user_id") is None:
            u_data.pop("clerk_user_id", None)
        existing = await db.users.find_one({"$or": [{"_id": u_id}, {"email": u.email.lower()}, {"username": u.username}]})
        if existing and existing.get("clerk_user_id") and existing["clerk_user_id"].startswith("user_2"):
            u_data.pop("clerk_user_id", None)
        await db.users.update_one(
            {"$or": [{"_id": u_id}, {"email": u.email.lower()}, {"username": u.username}]},
            {"$set": u_data, "$setOnInsert": {"_id": u_id}},
            upsert=True,
        )

    # 2. Games
    for g_data in GAMES_SEED:
        existing_g = await db.games.find_one({"slug": g_data["slug"]})
        if not existing_g:
            game = Game(**g_data)
            await db.games.insert_one(game.to_mongo())

    val_game = await db.games.find_one({"slug": "valorant"})
    val_game_id = str(val_game["_id"]) if val_game else "valorant"

    cs2_game = await db.games.find_one({"slug": "counter-strike-2"})
    cs2_game_id = str(cs2_game["_id"]) if cs2_game else "counter-strike-2"

    bgmi_game = await db.games.find_one({"slug": "bgmi"})
    bgmi_game_id = str(bgmi_game["_id"]) if bgmi_game else "bgmi"

    chess_game = await db.games.find_one({"slug": "chess"})
    chess_game_id = str(chess_game["_id"]) if chess_game else "chess"

    vortex_doc = await db.users.find_one({"username": "vortex_lead"})
    apex_doc = await db.users.find_one({"username": "apex_lead"})
    s1mple_doc = await db.users.find_one({"username": "s1mple_mumbai"})
    jonathan_doc = await db.users.find_one({"username": "jonathan_gaming"})
    org_doc = await db.users.find_one({"username": "arbiter_val"})
    admin_doc = await db.users.find_one({"email": "pk.4081988@gmail.com"}) or await db.users.find_one({"username": "super_admin"})

    vortex_id = str(vortex_doc["_id"]) if vortex_doc else "usr_vortex_lead"
    apex_id = str(apex_doc["_id"]) if apex_doc else "usr_apex_lead"
    s1mple_id = str(s1mple_doc["_id"]) if s1mple_doc else "usr_s1mple"
    jonathan_id = str(jonathan_doc["_id"]) if jonathan_doc else "usr_jonathan"
    org_id = str(org_doc["_id"]) if org_doc else "usr_arbiter_val"
    admin_id = str(admin_doc["_id"]) if admin_doc else "usr_pk_super_admin"

    # 3. Teams
    teams_to_seed = [
        Team(
            id="team_solaris_x",
            game_id=val_game_id,
            captain_id=vortex_id,
            name="Solaris X",
            tag="SLX",
            invite_code="SR7X4K",
            max_members=5,
            members=[
                TeamMember(user_id=vortex_id, role="CAPTAIN", game_handle="Vortex#APEX"),
                TeamMember(user_id="usr_phantom", role="MEMBER", game_handle="Phantom#999"),
                TeamMember(user_id="usr_omen", role="MEMBER", game_handle="Omen#IND"),
                TeamMember(user_id="usr_sova", role="MEMBER", game_handle="Sova#DARTS"),
                TeamMember(user_id="usr_jett", role="MEMBER", game_handle="Jett#WIND"),
            ],
        ),
        Team(
            id="team_apex_strikers",
            game_id=val_game_id,
            captain_id=apex_id,
            name="Apex Strikers",
            tag="APX",
            invite_code="APX999",
            max_members=5,
            members=[
                TeamMember(user_id=apex_id, role="CAPTAIN", game_handle="Apex#KING"),
            ],
        ),
        Team(
            id="team_navi_mumbai",
            game_id=cs2_game_id,
            captain_id=s1mple_id,
            name="NaVi Mumbai",
            tag="NVI",
            invite_code="NVI007",
            max_members=5,
            members=[
                TeamMember(user_id=s1mple_id, role="CAPTAIN", game_handle="s1mple#STEAM"),
            ],
        ),
        Team(
            id="team_godlike_arena",
            game_id=bgmi_game_id,
            captain_id=jonathan_id,
            name="GodLike Arena",
            tag="GDL",
            invite_code="GDL888",
            max_members=4,
            members=[
                TeamMember(user_id=jonathan_id, role="CAPTAIN", game_handle="Jonathan#BGMI"),
            ],
        ),
    ]

    for team_obj in teams_to_seed:
        team_data = team_obj.to_mongo()
        team_id = team_data.pop("_id")
        await db.teams.update_one(
            {"$or": [{"_id": team_id}, {"name": team_obj.name}]},
            {"$set": team_data, "$setOnInsert": {"_id": team_id}},
            upsert=True,
        )

    # 4. Tournaments
    tournaments_to_seed = [
        Tournament(
            id="tourn_val_open_01",
            slug="ametheus-valorant-open-01",
            game_id=val_game_id,
            game_slug="valorant",
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
            banner_url="https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200",
            badge_color="#E56A10",
            description="Tier-1 tactical competition featuring the best rosters in the sub-continent.",
            rules=["All players must have Vanguard running", "Coaches allowed only in timeouts", "128-tick Mumbai server"],
            schedule=[{"stage": "Check-in", "time": "7:30 PM", "details": "Mandatory check-in"}],
            prizes=[{"rank": "1st Place", "amount": "₹30,000", "share": "60%", "badge": "GOLD"}],
            bracket_rounds=[],
        ),
        Tournament(
            id="tourn_cs2_major_01",
            slug="ametheus-cs2-major-01",
            game_id=cs2_game_id,
            game_slug="counter-strike-2",
            organizer_id=org_id,
            title="AMETHEUS CS2 MUMBAI MASTERS",
            prize_pool_inr=75000,
            entry_fee_inr=100,
            status="LIVE",
            starts_at="Today, 9:00 PM IST",
            roster_lock_time="8:45 PM IST",
            server_region="Valve India Official (Mumbai)",
            max_teams=8,
            rank_requirement="Master Guardian or Higher",
            format="Single Elimination (MR12)",
            banner_url="https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200",
            badge_color="#F59E0B",
            description="Valve Anti-Cheat authenticated matches with hardware telemetry verification.",
            rules=["Subtick 128Hz server rate", "Overtime: MR3 10K max cash", "Coaching slots verified"],
            schedule=[{"stage": "Quarterfinals", "time": "9:00 PM", "details": "BO1 Matches"}],
            prizes=[{"rank": "1st Place", "amount": "₹45,000", "share": "60%", "badge": "GOLD"}],
            bracket_rounds=[],
        ),
        Tournament(
            id="tourn_bgmi_masters_01",
            slug="ametheus-bgmi-masters-01",
            game_id=bgmi_game_id,
            game_slug="bgmi",
            organizer_id=org_id,
            title="AMETHEUS BGMI PRO BATTLE ROYALE",
            prize_pool_inr=100000,
            entry_fee_inr=150,
            status="LIVE",
            starts_at="Tomorrow, 6:00 PM IST",
            roster_lock_time="5:30 PM IST",
            server_region="Krafton India Direct",
            max_teams=16,
            rank_requirement="Ace Tier or Higher",
            format="16 Squads Points Table",
            banner_url="https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200",
            badge_color="#E56A10",
            description="Battle Royale showdown across Erangel and Miramar with zero spectator delay.",
            rules=["Point system: 1pt per finish, 10pts WWCD", "No device emulators allowed", "Screen recording compulsory"],
            schedule=[{"stage": "Match 1", "time": "6:00 PM", "details": "Erangel Day 1"}],
            prizes=[{"rank": "1st Place", "amount": "₹60,000", "share": "60%", "badge": "GOLD"}],
            bracket_rounds=[],
        ),
        Tournament(
            id="tourn_chess_blitz_01",
            slug="ametheus-chess-blitz-01",
            game_id=chess_game_id,
            game_slug="chess",
            organizer_id=org_id,
            title="AMETHEUS BLITZ CHESS ARENA",
            prize_pool_inr=20000,
            entry_fee_inr=0,
            status="REGISTRATION_OPEN",
            starts_at="Tomorrow, 4:00 PM IST",
            roster_lock_time="3:45 PM IST",
            server_region="Global Arbiter Node",
            max_teams=32,
            min_team_size=1,
            max_team_size=1,
            rank_requirement="FIDE / Chess.com 1500+",
            format="Swiss 5 Rounds + Knockout",
            banner_url="https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200",
            badge_color="#3B82F6",
            description="Free-to-enter sanctioned blitz tournament with Stockfish fair play screening.",
            rules=["3+2 blitz time control", "No secondary assistance engines", "Touch-move rule enforced"],
            schedule=[{"stage": "Round 1", "time": "4:00 PM", "details": "Swiss Round 1"}],
            prizes=[{"rank": "1st Place", "amount": "₹12,000", "share": "60%", "badge": "GOLD"}],
            bracket_rounds=[],
        ),
    ]

    for t in tournaments_to_seed:
        t_data = t.to_mongo()
        t_id = t_data.pop("_id")
        await db.tournaments.update_one(
            {"slug": t.slug},
            {"$set": t_data, "$setOnInsert": {"_id": t_id}},
            upsert=True,
        )

    # 5. Registrations
    reg1 = TournamentRegistration(
        tournament_id="tourn_val_open_01",
        team_id="team_solaris_x",
        payment_status="PAID",
        order_id="order_escrow_demo_1",
        check_in_status="CHECKED_IN",
    )
    reg2 = TournamentRegistration(
        tournament_id="tourn_val_open_01",
        team_id="team_apex_strikers",
        payment_status="PAID",
        order_id="order_escrow_demo_2",
        check_in_status="CHECKED_IN",
    )
    for r in [reg1, reg2]:
        await db.tournament_registrations.update_one(
            {"tournament_id": r.tournament_id, "team_id": r.team_id},
            {"$setOnInsert": r.to_mongo()},
            upsert=True,
        )

    # 6. Admin Assignments
    for t_id in ["tourn_val_open_01", "tourn_cs2_major_01", "tourn_bgmi_masters_01"]:
        assign = TournamentAdminAssignment(
            admin_user_id=org_id,
            tournament_id=t_id,
            assigned_by=admin_id,
        )
        await db.tournament_admin_assignments.update_one(
            {"admin_user_id": assign.admin_user_id, "tournament_id": assign.tournament_id},
            {"$setOnInsert": assign.to_mongo()},
            upsert=True,
        )

    # 7. Matches (LIVE, PENDING_VERIFICATION, DISPUTED)
    match_val_live = Match(
        id="match_val_live_01",
        tournament_id="tourn_val_open_01",
        stage="Quarterfinals — Match 1 (BO3)",
        round_number=1,
        match_number=1,
        format="5v5 Competitive Custom (Ascent / Bind / Haven)",
        scheduled_time="Today, 8:30 PM IST",
        status="LIVE",
        team_a_id="team_solaris_x",
        team_b_id="team_apex_strikers",
        score_a=11,
        score_b=9,
        server_region="AWS Mumbai (ap-south-1) — 128 Tick",
        lobby_code="AMETHEUS-VAL-QF1-094",
        lobby_password="VANGUARD_ESCROW_99",
    )

    match_cs2_pending = Match(
        id="match_cs2_pending_02",
        tournament_id="tourn_cs2_major_01",
        stage="Semifinals — CS2 Mirage",
        round_number=2,
        match_number=1,
        format="5v5 Competitive MR12",
        scheduled_time="Today, 9:30 PM IST",
        status="PENDING_VERIFICATION",
        team_a_id="team_navi_mumbai",
        team_b_id="team_solaris_x",
        score_a=13,
        score_b=10,
        winner_team_id="team_navi_mumbai",
        server_region="Valve India Official (Mumbai)",
        lobby_code="AMETHEUS-CS2-SF1",
        lobby_password="VAC_LIVE_SECURE_7",
        evidence_url="https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=800",
        captain_notes="Map 1 defuse victory 13-10. Screenshot proof certified.",
    )

    match_bgmi_disputed = Match(
        id="match_bgmi_disputed_03",
        tournament_id="tourn_bgmi_masters_01",
        stage="Finals — Match 3 (Erangel)",
        round_number=1,
        match_number=3,
        format="Squads Battle Royale",
        scheduled_time="Today, 7:00 PM IST",
        status="DISPUTED",
        team_a_id="team_godlike_arena",
        team_b_id="team_solaris_x",
        score_a=18,
        score_b=14,
        server_region="Krafton India Direct",
        lobby_code="BGMI-CUSTOM-FINALS-3",
        lobby_password="ROOM_PASS_884",
        evidence_url="https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=800",
        dispute_notes="GodLike filed formal integrity dispute regarding spectator ping latency on drop zone.",
    )

    for m in [match_val_live, match_cs2_pending, match_bgmi_disputed]:
        m_data = m.to_mongo()
        m_id = m_data.pop("_id")
        await db.matches.update_one(
            {"_id": m_id},
            {"$set": m_data, "$setOnInsert": {"_id": m_id}},
            upsert=True,
        )

    # 8. Dispute in Arbitration Queue
    dispute_entry = Dispute(
        id="disp_bgmi_001",
        tournament_id="tourn_bgmi_masters_01",
        match_id="match_bgmi_disputed_03",
        submitted_by=jonathan_id,
        reason="UNAUTHORIZED_TELEMETRY",
        description="Formal complaint lodged regarding spectator screen delay. Circle 4 rotation contested.",
        evidence_url="https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=800",
        status="OPEN",
    )
    d_data = dispute_entry.to_mongo()
    d_id = d_data.pop("_id")
    await db.disputes.update_one(
        {"_id": d_id},
        {"$set": d_data, "$setOnInsert": {"_id": d_id}},
        upsert=True,
    )

    # 9. Audit Log
    await record_audit_log(
        db=db,
        operator="pk_admin",
        role="SUPER_ADMIN",
        action="INITIALIZE_PLATFORM_SEED",
        target="Database Seed",
        details="Enriched platform catalog with Valorant, CS2, BGMI, Chess, live matches, verification queue, and disputes",
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
