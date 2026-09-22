import asyncio
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_database

async def run_seed_dummy(db: AsyncIOMotorDatabase):
    now = datetime.now(timezone.utc)

    # 1. Dummy UPI Accounts
    dummy_upis = [
        {
            "id": "dummy_upi_01",
            "code": "UPI_DUMMY_01",
            "upi_id": "dummy.pay1@okaxis",
            "payee_name": "AMETHEUS Testing Alpha",
            "status": "ACTIVE",
            "payment_count": 5,
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "dummy_upi_02",
            "code": "UPI_DUMMY_02",
            "upi_id": "dummy.pay2@okhdfcbank",
            "payee_name": "AMETHEUS Testing Beta",
            "status": "ACTIVE",
            "payment_count": 2,
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "dummy_upi_03",
            "code": "UPI_DUMMY_03",
            "upi_id": "dummy.pay3@oksbi",
            "payee_name": "AMETHEUS Backup Testing",
            "status": "INACTIVE",
            "payment_count": 0,
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
    ]
    for u in dummy_upis:
        await db.upi_accounts.update_one({"code": u["code"]}, {"$set": u}, upsert=True)

    # 2. Dummy Users
    dummy_users = [
        {
            "id": "usr_dummy_01",
            "email": "dummy_player1@test.ametheus.gg",
            "username": "dummy_sniper",
            "display_name": "DummySniper99",
            "role": "PLAYER",
            "inr_balance_paise": 150000,
            "inr_reserved_paise": 50000,
            "inr_balance": "1500.0",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "usr_dummy_02",
            "email": "dummy_player2@test.ametheus.gg",
            "username": "dummy_rusher",
            "display_name": "DummyRusherX",
            "role": "PLAYER",
            "inr_balance_paise": 200000,
            "inr_reserved_paise": 0,
            "inr_balance": "2000.0",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
    ]
    for usr in dummy_users:
        await db.users.update_one({"id": usr["id"]}, {"$set": usr}, upsert=True)

    # 3. Dummy Teams
    bgmi_game = await db.games.find_one({"slug": "bgmi"})
    bgmi_id = str(bgmi_game.get("_id", bgmi_game.get("id"))) if bgmi_game else "game_bgmi_dummy"

    dummy_teams = [
        {
            "id": "team_dummy_01",
            "game_id": bgmi_id,
            "captain_id": "usr_dummy_01",
            "name": "Dummy Alpha Squad",
            "tag": "DAS",
            "invite_code": "DUMMY1",
            "max_members": 4,
            "max_substitutes": 1,
            "members": [
                {"id": "m1", "user_id": "usr_dummy_01", "role": "CAPTAIN", "game_handle": "DummyCap"},
            ],
            "substitutes": [],
            "payment_status": "PAID",
            "payment_id": "pay_dummy_01",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "team_dummy_02",
            "game_id": bgmi_id,
            "captain_id": "usr_dummy_02",
            "name": "Dummy Beta Warriors",
            "tag": "DBW",
            "invite_code": "DUMMY2",
            "max_members": 4,
            "max_substitutes": 1,
            "members": [
                {"id": "m2", "user_id": "usr_dummy_02", "role": "CAPTAIN", "game_handle": "DummyRush"},
            ],
            "substitutes": [],
            "payment_status": "PAYMENT_PENDING",
            "payment_id": "pay_dummy_02",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
    ]
    for tm in dummy_teams:
        await db.teams.update_one({"id": tm["id"]}, {"$set": tm}, upsert=True)

    # 4. Dummy Payments
    dummy_payments = [
        {
            "id": "pay_dummy_01",
            "payment_type": "GAME_ENTRY",
            "user_id": "usr_dummy_01",
            "game_id": bgmi_id,
            "game_name": "BGMI",
            "team_id": "team_dummy_01",
            "team_name": "Dummy Alpha Squad",
            "amount_paise": 10000,
            "amount_inr": 100.0,
            "payment_method": "UPI",
            "payment_account_code": "UPI_DUMMY_01",
            "upi_id": "dummy.pay1@okaxis",
            "payee_name": "AMETHEUS Testing Alpha",
            "utr": "DUMMY_UTR_111111",
            "status": "APPROVED",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "pay_dummy_02",
            "payment_type": "GAME_ENTRY",
            "user_id": "usr_dummy_02",
            "game_id": bgmi_id,
            "game_name": "BGMI",
            "team_id": "team_dummy_02",
            "team_name": "Dummy Beta Warriors",
            "amount_paise": 10000,
            "amount_inr": 100.0,
            "payment_method": "UPI",
            "payment_account_code": "UPI_DUMMY_02",
            "upi_id": "dummy.pay2@okhdfcbank",
            "payee_name": "AMETHEUS Testing Beta",
            "utr": "DUMMY_UTR_222222",
            "status": "PENDING",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        },
    ]
    for p in dummy_payments:
        await db.game_payments.update_one({"id": p["id"]}, {"$set": p}, upsert=True)

    # 5. Dummy Payouts
    dummy_payouts = [
        {
            "id": "payout_dummy_01",
            "payout_type": "WALLET_WITHDRAWAL",
            "user_id": "usr_dummy_01",
            "user_name": "DummySniper99",
            "amount_paise": 50000,
            "destination": "UPI",
            "upi_id": "dummysniper@okhdfcbank",
            "status": "PENDING",
            "is_dummy": True,
            "created_at": now,
            "updated_at": now,
        }
    ]
    for po in dummy_payouts:
        await db.payouts.update_one({"id": po["id"]}, {"$set": po}, upsert=True)

    return {
        "dummy_upis": len(dummy_upis),
        "dummy_users": len(dummy_users),
        "dummy_teams": len(dummy_teams),
        "dummy_payments": len(dummy_payments),
        "dummy_payouts": len(dummy_payouts),
    }

if __name__ == "__main__":
    db = get_database()
    res = asyncio.run(run_seed_dummy(db))
    print(f"[Seed Dummy] Successfully created dummy records: {res}")
