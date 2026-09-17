#!/usr/bin/env python3
"""
Standalone Universal Migration Script: SQL (SQLite / PostgreSQL) -> MongoDB
AMETHEUS Esports Platform

Transfers existing tables into MongoDB collections while mapping:
- Primary key `id` -> `_id` and `id`
- Team members embedded in `teams.members`
- Tournament JSON strings parsed into native BSON lists
- Preserves audit log hash chains and relationships

Usage:
  python scripts/migrate_sql_to_mongo.py
  python scripts/migrate_sql_to_mongo.py --database-url "postgresql://user:pass@host/db"
  python scripts/migrate_sql_to_mongo.py --sqlite-path ./ametheus.db --mongo-uri "mongodb+srv://..."
  python scripts/migrate_sql_to_mongo.py --dry-run
"""

import os
import sys
import json
import asyncio
import argparse
import sqlite3
from typing import Dict, Any, List, Optional
from motor.motor_asyncio import AsyncIOMotorClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

def safe_json(val, default):
    if not val:
        return default
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return default

def load_rows_from_sqlite(db_path: str) -> Dict[str, List[dict]]:
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")]
    data = {}
    for table in tables:
        cursor.execute(f'SELECT * FROM "{table}"')
        data[table] = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return data

def load_rows_from_postgres(db_url: str) -> Dict[str, List[dict]]:
    from sqlalchemy import create_engine, inspect, text
    # Fix Render postgres:// prefix if present
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    engine = create_engine(db_url)
    insp = inspect(engine)
    tables = [t for t in insp.get_table_names() if not t.startswith("alembic_")]
    data = {}
    with engine.connect() as conn:
        for table in tables:
            result = conn.execute(text(f'SELECT * FROM "{table}"'))
            cols = result.keys()
            data[table] = [dict(zip(cols, row)) for row in result.fetchall()]
    return data

async def run_migration(args):
    from app.core.config import settings
    mongo_uri = args.mongo_uri or settings.MONGODB_URI
    mongo_db_name = args.mongo_db or settings.MONGODB_DB_NAME
    sqlite_path = args.sqlite_path or os.getenv("SQLITE_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ametheus.db")))
    database_url = args.database_url or os.getenv("DATABASE_URL")

    print("=" * 65)
    print("      AMETHEUS: SQL to MongoDB Data Migration Utility")
    print("=" * 65)
    print(f"Target MongoDB URI:  {mongo_uri.split('@')[-1] if '@' in mongo_uri else mongo_uri}")
    print(f"Target Database:     {mongo_db_name}")

    # Determine source
    data: Dict[str, List[dict]] = {}
    source_type = "None"

    if database_url and ("postgres" in database_url.lower()):
        print(f"Source Database:     PostgreSQL ({database_url.split('@')[-1] if '@' in database_url else 'configured URL'})")
        try:
            data = load_rows_from_postgres(database_url)
            source_type = "PostgreSQL"
        except Exception as e:
            print(f"[ERROR] Failed to load data from PostgreSQL: {e}")
            print("[INFO] Falling back to SQLite...")

    if not data and os.path.exists(sqlite_path):
        print(f"Source Database:     SQLite ({sqlite_path})")
        data = load_rows_from_sqlite(sqlite_path)
        source_type = "SQLite"

    if not data:
        print("[NOTICE] No existing SQL database found with tables.")
        print("[INFO] Initializing MongoDB with official default platform seed...")
        client = AsyncIOMotorClient(mongo_uri)
        db = client[mongo_db_name]
        from app.core.database import init_db_indexes
        from app.seed import populate_seed_data
        await init_db_indexes(db)
        await populate_seed_data(db)
        print("[SUCCESS] MongoDB initialized with default game platform seed!")
        return

    print(f"\n[OK] Connected to {source_type}. Discovered tables:")
    for tbl, rows in data.items():
        print(f"  - {tbl:30} : {len(rows):4} rows")

    if args.dry_run:
        print("\n[DRY RUN] Dry run mode enabled. No data written to MongoDB.")
        return

    print("\nConnecting to MongoDB...")
    client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=8000)
    db = client[mongo_db_name]

    # Initialize collections and indexes
    from app.core.database import init_db_indexes
    await init_db_indexes(db)

    migrated_counts = {}

    # 1. Users
    users = data.get("users", [])
    if users:
        for u in users:
            d = dict(u)
            d["_id"] = d.get("id")
            await db.users.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["users"] = len(users)

    # 2. Games
    games = data.get("games", [])
    if games:
        for g in games:
            d = dict(g)
            d["_id"] = d.get("id")
            await db.games.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["games"] = len(games)

    # 3. Teams & Team Members
    team_members_by_team: Dict[str, List[dict]] = {}
    for tm in data.get("team_members", []):
        m = dict(tm)
        tid = m.get("team_id")
        if tid:
            team_members_by_team.setdefault(tid, []).append(m)

    teams = data.get("teams", [])
    if teams:
        for t in teams:
            d = dict(t)
            d["_id"] = d.get("id")
            d["members"] = team_members_by_team.get(d["id"], [])
            await db.teams.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["teams"] = len(teams)

    # 4. Tournaments
    tournaments = data.get("tournaments", [])
    if tournaments:
        for t in tournaments:
            d = dict(t)
            d["_id"] = d.get("id")
            d["rules"] = safe_json(d.pop("rules_json", None), [])
            d["schedule"] = safe_json(d.pop("schedule_json", None), [])
            d["prizes"] = safe_json(d.pop("prizes_json", None), [])
            d["bracket_rounds"] = safe_json(d.pop("bracket_json", None), [])
            await db.tournaments.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["tournaments"] = len(tournaments)

    # 5. Tournament Registrations
    registrations = data.get("tournament_registrations", [])
    if registrations:
        for r in registrations:
            d = dict(r)
            d["_id"] = d.get("id")
            await db.tournament_registrations.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["tournament_registrations"] = len(registrations)

    # 6. Matches
    matches = data.get("matches", [])
    if matches:
        for m in matches:
            d = dict(m)
            d["_id"] = d.get("id")
            await db.matches.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["matches"] = len(matches)

    # 7. Tournament Admin Assignments
    admin_assigns = data.get("tournament_admin_assignments", [])
    if admin_assigns:
        for a in admin_assigns:
            d = dict(a)
            d["_id"] = d.get("id")
            await db.tournament_admin_assignments.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["tournament_admin_assignments"] = len(admin_assigns)

    # 8. Disputes
    disputes = data.get("disputes", [])
    if disputes:
        for disp in disputes:
            d = dict(disp)
            d["_id"] = d.get("id")
            await db.disputes.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["disputes"] = len(disputes)

    # 9. Notifications
    notifications = data.get("notifications", [])
    if notifications:
        for n in notifications:
            d = dict(n)
            d["_id"] = d.get("id")
            await db.notifications.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["notifications"] = len(notifications)

    # 10. Audit Logs
    audit_logs = data.get("audit_logs", [])
    if audit_logs:
        for l in audit_logs:
            d = dict(l)
            d["_id"] = d.get("id")
            await db.audit_logs.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
        migrated_counts["audit_logs"] = len(audit_logs)

    print("\n" + "=" * 65)
    print("                 MIGRATION COMPLETED SUCCESSFULLY")
    print("=" * 65)
    for col, count in migrated_counts.items():
        print(f"  [OK] {col:30} : {count:4} documents stored")
    print("=" * 65)

def main():
    parser = argparse.ArgumentParser(description="Migrate AMETHEUS SQL data to MongoDB")
    parser.add_argument("--database-url", help="PostgreSQL connection string (e.g. postgresql://user:pass@host/db)")
    parser.add_argument("--sqlite-path", help="Path to SQLite database file (defaults to ametheus.db)")
    parser.add_argument("--mongo-uri", help="MongoDB connection URI (defaults to MONGODB_URI env var or mongodb://localhost:27017)")
    parser.add_argument("--mongo-db", help="MongoDB database name (defaults to MONGODB_DB_NAME env var or ametheus)")
    parser.add_argument("--dry-run", action="store_true", help="Preview records without writing to MongoDB")
    args = parser.parse_args()

    asyncio.run(run_migration(args))

if __name__ == "__main__":
    main()
