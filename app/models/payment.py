from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class UpiAccount(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    code: str  # e.g., UPI001, UPI002
    upi_id: str  # e.g., platform@okhdfcbank
    payee_name: str
    qr_url: Optional[str] = None
    display_name: Optional[str] = None
    status: str = "ACTIVE"  # ACTIVE, INACTIVE
    payment_count: int = 0
    is_dummy: bool = False
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)

class GamePayment(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    payment_type: str = "GAME_ENTRY"  # GAME_ENTRY, WALLET_TOPUP
    user_id: str
    game_id: Optional[str] = None
    game_name: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    amount_paise: int
    amount_inr: float
    payment_method: str = "UPI"  # UPI, WALLET

    # Snapshot of assigned UPI account (permanent & immutable)
    payment_account_id: Optional[str] = None
    payment_account_code: Optional[str] = None
    upi_id: Optional[str] = None
    payee_name: Optional[str] = None
    qr_url: Optional[str] = None
    upi_uri: Optional[str] = None

    # Player submission
    utr: Optional[str] = None  # Actual transaction reference from player's UPI app
    screenshot_url: Optional[str] = None
    status: str = "PENDING"  # PENDING, APPROVED, REJECTED
    rejection_reason: Optional[str] = None
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    is_dummy: bool = False
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)

class WalletLedger(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    user_id: str
    txn_type: str  # WALLET_TOPUP, GAME_ENTRY_PAYMENT, GAME_WINNING, WALLET_WITHDRAWAL, ADMIN_ADJUSTMENT
    amount_paise: int  # Positive for credit, negative for debit
    balance_before_paise: int
    balance_after_paise: int
    reference_id: Optional[str] = None
    description: str
    admin_id: Optional[str] = None
    is_dummy: bool = False
    created_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)

class Payout(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    payout_type: str  # GAME_WINNING, WALLET_WITHDRAWAL
    user_id: str
    user_name: Optional[str] = None
    amount_paise: int
    destination: str = "UPI"  # UPI, WALLET
    upi_id: Optional[str] = None
    game_id: Optional[str] = None
    game_name: Optional[str] = None
    rank_or_placement: Optional[str] = None
    status: str = "PENDING"  # PENDING, PAID, REJECTED
    outgoing_utr: Optional[str] = None
    admin_id: Optional[str] = None
    paid_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    is_dummy: bool = False
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)
