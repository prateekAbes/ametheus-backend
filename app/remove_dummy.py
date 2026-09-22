import asyncio
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_database

async def run_remove_dummy(db: AsyncIOMotorDatabase):
    """
    Finds and deletes all records explicitly tagged with is_dummy: True.
    Never deletes production records.
    """
    collections = [
        "users",
        "teams",
        "upi_accounts",
        "game_payments",
        "wallet_ledger",
        "payouts",
        "games",
    ]
    results = {}
    for col_name in collections:
        col = db[col_name]
        res = await col.delete_many({"is_dummy": True})
        results[col_name] = res.deleted_count

    return results

if __name__ == "__main__":
    db = get_database()
    res = asyncio.run(run_remove_dummy(db))
    print(f"[Remove Dummy] Safely deleted dummy records: {res}")
