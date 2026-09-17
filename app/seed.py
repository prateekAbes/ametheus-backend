import asyncio
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.core.database import engine, Base, AsyncSessionLocal
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.tournament import Tournament, TournamentRegistration
from app.models.match import Match
from app.models.admin import TournamentAdminAssignment, AuditLog

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

async def populate_seed_data(db: AsyncSession):
    # 1. Users
    super_admin = User(
        id="usr_super_admin",
        clerk_user_id="user_super_admin_clerk",
        email="admin@ametheus.gg",
        username="super_admin",
        display_name="Super Admin",
        role="SUPER_ADMIN",
    )
    tournament_admin = User(
        id="usr_arbiter_val",
        clerk_user_id="user_arbiter_val_clerk",
        email="arbiter@ametheus.gg",
        username="arbiter_val",
        display_name="Valorant Arbiter",
        role="ORGANIZER",
    )
    captain_vortex = User(
        id="usr_vortex_lead",
        clerk_user_id="user_vortex_lead_clerk",
        email="vortex@solaris.gg",
        username="vortex_lead",
        display_name="Vortex Lead",
        role="PLAYER",
        riot_id="Vortex#APEX",
        steam_id="STEAM_0:1:88291039",
    )
    phantom = User(
        id="usr_phantom",
        email="phantom@solaris.gg",
        username="phantom",
        display_name="Phantom",
        role="PLAYER",
    )
    omen = User(
        id="usr_omen",
        email="omen@solaris.gg",
        username="omen",
        display_name="Omen",
        role="PLAYER",
    )
    sova = User(
        id="usr_sova",
        email="sova@solaris.gg",
        username="sova",
        display_name="Sova",
        role="PLAYER",
    )
    jett = User(
        id="usr_jett",
        email="jett@solaris.gg",
        username="jett",
        display_name="Jett",
        role="PLAYER",
    )
    apex_lead = User(
        id="usr_apex_lead",
        email="lead@apexstrikers.gg",
        username="apex_lead",
        display_name="Apex Leader",
        role="PLAYER",
    )

    all_users = (await db.execute(select(User))).scalars().all()
    existing_user_ids = {u.id for u in all_users}
    existing_emails = {u.email for u in all_users}
    existing_usernames = {u.username for u in all_users}

    users_to_add = []
    for u in [super_admin, tournament_admin, captain_vortex, phantom, omen, sova, jett, apex_lead]:
        if u.id not in existing_user_ids and u.email not in existing_emails and u.username not in existing_usernames:
            users_to_add.append(u)
    if users_to_add:
        db.add_all(users_to_add)
        await db.flush()

    # Re-fetch users map by username to resolve actual IDs
    user_by_username = {u.username: u for u in (await db.execute(select(User))).scalars().all()}
    admin_user = user_by_username.get("super_admin") or super_admin
    org_user = user_by_username.get("arbiter_val") or tournament_admin
    vortex_user = user_by_username.get("vortex_lead") or captain_vortex
    apex_user = user_by_username.get("apex_lead") or apex_lead

    # 2. Games
    existing_games = {g.slug: g for g in (await db.execute(select(Game))).scalars().all()}
    game_map = dict(existing_games)
    for g_data in GAMES_SEED:
        if g_data["slug"] not in game_map:
            game = Game(**g_data)
            db.add(game)
            await db.flush()
            game_map[game.slug] = game

    # 3. Teams
    val_game = game_map.get("valorant") or list(game_map.values())[0]
    existing_teams = {t.name: t for t in (await db.execute(select(Team))).scalars().all()}

    solaris_x = existing_teams.get("Solaris X")
    if not solaris_x:
        solaris_x = Team(
            game_id=val_game.id,
            captain_id=vortex_user.id,
            name="Solaris X",
            tag="SLX",
            invite_code="SR7X4K",
            max_members=5,
        )
        db.add(solaris_x)
        await db.flush()

        phantom_u = user_by_username.get("phantom") or phantom
        omen_u = user_by_username.get("omen") or omen
        sova_u = user_by_username.get("sova") or sova
        jett_u = user_by_username.get("jett") or jett

        mems = [
            TeamMember(team_id=solaris_x.id, user_id=vortex_user.id, role="CAPTAIN", game_handle="Vortex#APEX"),
            TeamMember(team_id=solaris_x.id, user_id=phantom_u.id, role="MEMBER", game_handle="Phantom#999"),
            TeamMember(team_id=solaris_x.id, user_id=omen_u.id, role="MEMBER", game_handle="Omen#IND"),
            TeamMember(team_id=solaris_x.id, user_id=sova_u.id, role="MEMBER", game_handle="Sova#DARTS"),
            TeamMember(team_id=solaris_x.id, user_id=jett_u.id, role="MEMBER", game_handle="Jett#WIND"),
        ]
        db.add_all(mems)

    apex_strikers = existing_teams.get("Apex Strikers")
    if not apex_strikers:
        apex_strikers = Team(
            game_id=val_game.id,
            captain_id=apex_user.id,
            name="Apex Strikers",
            tag="APX",
            invite_code="APX999",
            max_members=5,
        )
        db.add(apex_strikers)
        await db.flush()

    # 4. Tournaments
    existing_tourns = {t.slug: t for t in (await db.execute(select(Tournament))).scalars().all()}
    val_tourn = existing_tourns.get("ametheus-valorant-open-01")
    if not val_tourn:
        val_tourn = Tournament(
            slug="ametheus-valorant-open-01",
            game_id=val_game.id,
            organizer_id=org_user.id,
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
            rules_json=json.dumps(["All players must have Vanguard running", "Coaches allowed only in timeouts", "128-tick Mumbai server"]),
            schedule_json=json.dumps([{"stage": "Check-in", "time": "7:30 PM", "details": "Mandatory check-in"}]),
            prizes_json=json.dumps([{"rank": "1st Place", "amount": "₹30,000", "share": "60%", "badge": "GOLD"}]),
            bracket_json=json.dumps([]),
        )
        db.add(val_tourn)
        await db.flush()

        reg1 = TournamentRegistration(
            tournament_id=val_tourn.id,
            team_id=solaris_x.id,
            payment_status="PAID",
            order_id="order_rzp_demo_1",
            check_in_status="CHECKED_IN",
        )
        reg2 = TournamentRegistration(
            tournament_id=val_tourn.id,
            team_id=apex_strikers.id,
            payment_status="PAID",
            order_id="order_rzp_demo_2",
            check_in_status="CHECKED_IN",
        )
        db.add_all([reg1, reg2])

        assign = TournamentAdminAssignment(
            admin_user_id=org_user.id,
            tournament_id=val_tourn.id,
            assigned_by=admin_user.id,
        )
        db.add(assign)

        match1 = Match(
            tournament_id=val_tourn.id,
            stage="Quarterfinals — Match 1 (BO3)",
            round_number=1,
            match_number=1,
            format="5v5 Competitive Custom (Ascent / Bind / Haven)",
            scheduled_time="Today, 8:30 PM IST",
            status="LIVE",
            team_a_id=solaris_x.id,
            team_b_id=apex_strikers.id,
            score_a=11,
            score_b=9,
            server_region="AWS Mumbai (ap-south-1) — 128 Tick",
            lobby_code="AMETHEUS-VAL-QF1-094",
            lobby_password="VANGUARD_ESCROW_99",
        )
        db.add(match1)

    # 5. Audit Log
    from app.services.audit import record_audit_log
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="INITIALIZE_PLATFORM_SEED",
        target="Database Seed",
        details="Initial platform seeding with official tournament and teams",
    )
    await db.commit()

async def seed_if_empty():
    try:
        async with AsyncSessionLocal() as db:
            g_count = (await db.execute(select(func.count(Game.id)))).scalar() or 0
            t_count = (await db.execute(select(func.count(Tournament.id)))).scalar() or 0
            if g_count == 0 or t_count == 0:
                print(f"[AMETHEUS] Database has {g_count} games and {t_count} tournaments. Running idempotent seeding...")
                await populate_seed_data(db)
                print("[AMETHEUS] Initial catalog & tournaments seeded successfully!")
            else:
                print(f"[AMETHEUS] Database contains {g_count} games and {t_count} tournaments.")
    except Exception as e:
        print(f"[AMETHEUS] Seed check notice: {e}")

async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await populate_seed_data(db)
    print("Database full re-seed completed successfully!")

if __name__ == "__main__":
    asyncio.run(seed())
