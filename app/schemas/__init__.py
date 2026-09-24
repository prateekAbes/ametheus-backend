from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict

# User
class UserBase(BaseModel):
    email: str
    username: str
    role: str = "PLAYER"
    display_name: Optional[str] = None
    clerk_user_id: Optional[str] = None
    avatar_url: Optional[str] = None
    riot_id: Optional[str] = None
    steam_id: Optional[str] = None

class UserCreate(UserBase):
    id: str

class UserResponse(UserBase):
    id: str
    inr_balance: str = "0"
    inr_balance_paise: int = 0
    inr_reserved_paise: int = 0
    model_config = ConfigDict(from_attributes=True)

# Game
class GameResponse(BaseModel):
    id: str
    slug: str
    name: str
    category: str
    platform: str
    tag: str
    is_team_game: bool
    min_team_size: int
    max_team_size: int
    max_substitutes: int = 1
    entry_fee_inr: int = 0
    prize_pool_inr: int = 0
    play_url: Optional[str] = None
    banner_url: str
    logo_url: str
    description: str
    server_region: str
    anti_cheat: str
    match_protocol: str
    accent_color: Optional[str] = "#E56A10"
    status: str = "ACTIVE"
    expected_release_date: Optional[str] = None
    announcement_text: Optional[str] = None
    hero_media_url: Optional[str] = None
    hero_media_type: Optional[str] = "gif"
    hero_media_enabled: bool = True
    gallery: List[Dict[str, Any]] = []
    teams_count: int = 0
    model_config = ConfigDict(from_attributes=True)

class GameCreate(BaseModel):
    slug: str
    name: str
    category: str
    platform: str
    tag: str
    is_team_game: bool = True
    min_team_size: int = 1
    max_team_size: int = 5
    max_substitutes: int = 1
    entry_fee_inr: int = 0
    prize_pool_inr: int = 0
    play_url: Optional[str] = None
    banner_url: str = "https://images.unsplash.com/photo-1542751371-adc38448a05e"
    logo_url: str = "https://images.unsplash.com/photo-1542751371-adc38448a05e"
    description: str = ""
    server_region: str = "AWS Mumbai (ap-south-1)"
    anti_cheat: str = "Standard Protocol"
    match_protocol: str = "Custom Lobby"
    accent_color: Optional[str] = "#E56A10"
    status: Optional[str] = "ACTIVE"
    expected_release_date: Optional[str] = None
    announcement_text: Optional[str] = None
    hero_media_url: Optional[str] = None
    hero_media_type: Optional[str] = "gif"
    hero_media_enabled: Optional[bool] = True
    gallery: Optional[List[Dict[str, Any]]] = None

class GameUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    platform: Optional[str] = None
    tag: Optional[str] = None
    is_team_game: Optional[bool] = None
    min_team_size: Optional[int] = None
    max_team_size: Optional[int] = None
    max_substitutes: Optional[int] = None
    entry_fee_inr: Optional[int] = None
    prize_pool_inr: Optional[int] = None
    play_url: Optional[str] = None
    banner_url: Optional[str] = None
    logo_url: Optional[str] = None
    description: Optional[str] = None
    server_region: Optional[str] = None
    anti_cheat: Optional[str] = None
    match_protocol: Optional[str] = None
    accent_color: Optional[str] = None
    status: Optional[str] = None
    expected_release_date: Optional[str] = None
    announcement_text: Optional[str] = None
    hero_media_url: Optional[str] = None
    hero_media_type: Optional[str] = None
    hero_media_enabled: Optional[bool] = None
    gallery: Optional[List[Dict[str, Any]]] = None

# Team
class TeamMemberResponse(BaseModel):
    id: str
    user_id: str
    username: str
    role: str
    game_handle: str
    model_config = ConfigDict(from_attributes=True)

class TeamCreate(BaseModel):
    game_slug: str
    name: str
    tag: str
    captain_handle: str

class TeamJoin(BaseModel):
    invite_code: str
    player_handle: str
    is_substitute: bool = False

class TeamResponse(BaseModel):
    id: str
    game_slug: str
    name: str
    tag: str
    invite_code: str
    captain_id: str
    captain_username: str
    max_members: int
    max_substitutes: int = 1
    payment_status: str = "UNPAID"
    payment_id: Optional[str] = None
    members: List[TeamMemberResponse] = []
    substitutes: List[TeamMemberResponse] = []
    model_config = ConfigDict(from_attributes=True)

# Payment & UPI
class UpiAccountCreate(BaseModel):
    code: str
    upi_id: str
    payee_name: str
    qr_url: Optional[str] = None
    display_name: Optional[str] = None
    status: str = "ACTIVE"

class UpiAccountUpdate(BaseModel):
    upi_id: Optional[str] = None
    payee_name: Optional[str] = None
    qr_url: Optional[str] = None
    display_name: Optional[str] = None
    status: Optional[str] = None

class UpiAccountResponse(BaseModel):
    id: str
    code: str
    upi_id: str
    payee_name: str
    qr_url: Optional[str] = None
    display_name: Optional[str] = None
    status: str
    payment_count: int
    created_at: Any
    model_config = ConfigDict(from_attributes=True)

class GamePaymentInitiateRequest(BaseModel):
    game_slug: str
    team_id: Optional[str] = None
    payment_method: str = "UPI"  # UPI, WALLET

class GamePaymentInitiateResponse(BaseModel):
    payment_id: str
    game_id: str
    game_name: str
    team_id: Optional[str] = None
    amount_inr: float
    amount_paise: int
    payment_method: str
    status: str
    wallet_discount_inr: Optional[float] = None
    # For UPI payment:
    payment_account_code: Optional[str] = None
    upi_id: Optional[str] = None
    payee_name: Optional[str] = None
    qr_url: Optional[str] = None
    upi_uri: Optional[str] = None

class SubmitUtrRequest(BaseModel):
    utr: str
    screenshot_url: Optional[str] = None

class WalletTopupInitiateRequest(BaseModel):
    amount_inr: float

class WalletWithdrawRequest(BaseModel):
    amount_inr: float
    upi_id: str

class WalletBalanceResponse(BaseModel):
    available_paise: int
    available_inr: float
    reserved_paise: int
    reserved_inr: float
    total_paise: int
    total_inr: float

# Admin
class AuditLogResponse(BaseModel):
    id: str
    timestamp: str
    operator: str
    role: str
    action: str
    target: str
    ip_address: str
    prev_hash: Optional[str] = None
    record_hash: Optional[str] = None

# Notification
class NotificationResponse(BaseModel):
    id: str
    type: str
    title: str
    message: str
    is_read: bool
    link: Optional[str] = None
    created_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class NotificationListResponse(BaseModel):
    items: List[NotificationResponse]
    unread_count: int

# Admin Assignments & Disputes
class AdminAssignmentCreate(BaseModel):
    admin_user_id: str

class DisputeResolve(BaseModel):
    decision: str
    resolution_notes: Optional[str] = None

class DisputeResponse(BaseModel):
    id: str
    match_id: Optional[str] = None
    raised_by: str
    reason: str
    status: str
    resolution_notes: Optional[str] = None
    created_at: Optional[str] = None

class LeaderboardEntryResponse(BaseModel):
    id: str
    name: str
    tag: str
    type: str = "SQUAD"
    game_slug: str
    game_name: str
    captain: Optional[str] = None
    logo_url: Optional[str] = None
    win_rate: Optional[Any] = "0%"
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    rating: int = 1200
    earnings: Optional[Any] = "₹0"
    form: List[str] = []
    is_verified: bool = False
    category: str = "overall"
    rank: int = 1
    created_at: Optional[Any] = None
    model_config = ConfigDict(from_attributes=True, extra="allow")
