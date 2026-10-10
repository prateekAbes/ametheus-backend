from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, model_validator

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
    preferences: Optional[Dict[str, Any]] = None
    privacy_settings: Optional[Dict[str, Any]] = None
    has_chosen_username: bool = True
    needs_username: bool = False
    known_ips: Optional[List[str]] = None
    last_login_ip: Optional[str] = None
    last_login_at: Optional[datetime] = None

class UserCreate(UserBase):
    id: str

class UserResponse(UserBase):
    id: str
    inr_balance: str = "0"
    inr_balance_paise: int = 0
    inr_reserved_paise: int = 0
    model_config = ConfigDict(from_attributes=True)

# Crest
class CrestBase(BaseModel):
    name: str
    description: Optional[str] = None
    image_url: str
    is_active: bool = True
    category: Optional[str] = "OPERATOR"

class CrestCreate(CrestBase):
    pass

class CrestUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    is_active: Optional[bool] = None
    category: Optional[str] = None

class CrestResponse(CrestBase):
    id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

# Game
class GameResponse(BaseModel):
    id: str
    slug: str
    name: str
    category: str
    genre_id: Optional[str] = None
    genre_name: Optional[str] = None
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
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
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
    genre_id: Optional[str] = None
    genre_name: Optional[str] = None
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
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
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
    genre_id: Optional[str] = None
    genre_name: Optional[str] = None
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
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
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

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Resolve game slug / game id
            slug = data.get("game_slug") or data.get("game_id")
            if slug:
                data["game_slug"] = str(slug).strip()
            # Resolve captain handle / game handle / player handle / handle
            handle = data.get("captain_handle") or data.get("game_handle") or data.get("player_handle") or data.get("handle")
            if handle:
                data["captain_handle"] = str(handle).strip()
            elif not data.get("captain_handle"):
                data["captain_handle"] = "Contender"
        return data

class TeamJoin(BaseModel):
    invite_code: str
    player_handle: str
    is_substitute: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            handle = data.get("player_handle") or data.get("game_handle") or data.get("handle")
            if handle:
                data["player_handle"] = str(handle).strip()
            elif not data.get("player_handle"):
                data["player_handle"] = "Contender"
            if "invite_code" in data:
                data["invite_code"] = str(data["invite_code"]).strip().upper()
        return data

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
    status: str = "ACTIVE"
    is_disbanded: bool = False
    disbanded_at: Optional[Any] = None
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
    status: str = "ACTIVE"
    payment_count: int = 0
    created_at: Optional[Any] = None
    model_config = ConfigDict(from_attributes=True, extra="ignore")

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
    timestamp: Optional[str] = None
    created_at: Optional[str] = None
    operator: str
    role: str
    action: str
    target: str
    ip_address: str
    details: Optional[str] = None
    prev_hash: Optional[str] = None
    record_hash: Optional[str] = None
    is_archived: bool = False
    archived_at: Optional[str] = None
    archived_by: Optional[str] = None
    archive_reason: Optional[str] = None

# Notification
class NotificationResponse(BaseModel):
    id: str
    type: str
    category: str = "SYSTEM"
    title: str
    message: str
    is_read: bool
    link: Optional[str] = None
    created_at: Optional[str] = None
    is_archived: bool = False
    archived_at: Optional[str] = None
    archived_by: Optional[str] = None
    archive_reason: Optional[str] = None
    is_cleared: bool = False
    sender_role: Optional[str] = None
    sender_name: Optional[str] = None
    label: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class NotificationListResponse(BaseModel):
    items: List[NotificationResponse]
    unread_count: int

# Tournament
class TournamentResponse(BaseModel):
    id: str
    slug: str
    organizer_id: Optional[str] = None
    title: Optional[str] = "Championship"
    game_slug: Optional[str] = "valorant"
    game_name: Optional[str] = "Valorant"
    prize_pool: Optional[str] = "₹0"
    prize_pool_inr: Optional[int] = 0
    entry_fee: Optional[str] = "₹0 / Free"
    entry_fee_inr: Optional[int] = 0
    status: Optional[str] = "REGISTRATION_OPEN"
    expected_start_date: Optional[str] = None
    registration_open_date: Optional[str] = None
    announcement_text: Optional[str] = None
    starts_at: Optional[str] = "TBD"
    roster_lock_time: Optional[str] = "TBD"
    server_region: Optional[str] = "AWS Mumbai (ap-south-1)"
    registered_teams: Optional[int] = 0
    max_teams: Optional[int] = 16
    min_team_size: Optional[int] = 1
    max_team_size: Optional[int] = 5
    rank_requirement: Optional[str] = "Open to All Ranks"
    format: Optional[str] = "Single Elimination"
    tournament_type: Optional[str] = "Single Elimination"
    match_format: Optional[str] = "Single Elimination"
    series_format: Optional[str] = "BO1"
    has_live_matches: bool = False
    live_matches_count: int = 0
    live_matches: List[Dict[str, Any]] = []
    stage_formats: Dict[str, str] = Field(default_factory=dict)
    registration_deadline: Optional[str] = None
    check_in_window_minutes: Optional[int] = 15
    banner_url: Optional[str] = ""
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
    logo_url: Optional[str] = None
    badge_color: Optional[str] = "#E56A10"
    description: Optional[str] = ""
    rules: List[str] = []
    schedule: List[Dict[str, Any]] = []
    prizes: List[Dict[str, Any]] = []
    bracket_rounds: List[Dict[str, Any]] = []
    registered_rosters: List[Dict[str, Any]] = []
    is_featured: Optional[bool] = False
    model_config = ConfigDict(from_attributes=True, extra="allow")

class TournamentRegisterRequest(BaseModel):
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    player_handle: Optional[str] = None
    game_id: Optional[str] = None
    in_game_id: Optional[str] = None
    idempotency_key: Optional[str] = None

class TournamentRegisterUpiResponse(BaseModel):
    payment_id: str
    registration_id: str
    tournament_id: str
    tournament_title: str
    amount_inr: float
    amount_paise: int
    account_code: Optional[str] = None
    upi_id: Optional[str] = None
    payee_name: Optional[str] = None
    qr_url: Optional[str] = None
    upi_uri: Optional[str] = None
    status: str = "INITIATED"

class TournamentUpiDetailsResponse(BaseModel):
    tournament_id: str
    tournament_title: str
    amount_inr: float
    amount_paise: int
    account_code: Optional[str] = None
    upi_id: str
    payee_name: str
    qr_url: Optional[str] = None
    upi_uri: str

class SubmitTournamentUtrRequest(BaseModel):
    payment_id: Optional[str] = None
    utr: str
    team_id: Optional[str] = None
    account_code: Optional[str] = None
    screenshot_url: Optional[str] = None
    game_id: Optional[str] = None
    in_game_id: Optional[str] = None

class TournamentRegistrationStatusResponse(BaseModel):
    is_registered: bool
    registration: Optional[Dict[str, Any]] = None

class TournamentStageFormatsUpdate(BaseModel):
    tournament_type: Optional[str] = None
    match_format: Optional[str] = None
    stage_formats: Optional[Dict[str, str]] = None
    registration_deadline: Optional[str] = None

class TournamentFormatUpdatePayload(BaseModel):
    format: str  # Knockout, Round Robin, Combination, Swiss System, Ladder Tournament

class AdminSendNotificationRequest(BaseModel):
    user_ids: List[str]
    title: str
    message: str
    notification_type: Optional[str] = "ADMIN"
    category: Optional[str] = None
    link: Optional[str] = None

# Match
class MatchResponse(BaseModel):
    id: str
    tournament_id: str
    tournament_title: str
    game_slug: str
    game_name: str
    stage: str
    match_type: Optional[str] = "QUARTER_FINAL"
    format: str = "BO1"
    series_format: Optional[str] = "BO1"
    scheduled_time: str
    scheduled_at: Optional[str] = None
    live_link: Optional[str] = None
    youtube_live_url: Optional[str] = None
    order: Optional[int] = 0
    status: str
    roster_status: Optional[str] = "PUBLISHED"
    round_name: Optional[str] = None
    round_number: Optional[int] = 1
    match_number: Optional[int] = 1
    scheduled_date: Optional[str] = None
    server_region: str
    lobby_code: Optional[str] = None
    lobby_password: Optional[str] = None
    join_info_enabled: Optional[bool] = False
    match_message: Optional[str] = None
    team_a: Optional[Dict[str, Any]] = None
    team_b: Optional[Dict[str, Any]] = None
    winner_team_id: Optional[str] = None
    check_in_status: Optional[Dict[str, bool]] = None
    evidence_url: Optional[str] = None
    dispute_notes: Optional[str] = None
    model_config = ConfigDict(from_attributes=True, extra="allow")

class AdminMatchPairRequest(BaseModel):
    tournament_id: str
    team_a_id: str
    team_b_id: str
    stage: str = "Quarterfinals"
    match_type: Optional[str] = "QUARTER_FINAL"
    series_format: Optional[str] = "BO1"
    match_format: Optional[str] = "BO1"
    scheduled_at: Optional[str] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None
    live_link: Optional[str] = None
    youtube_live_url: Optional[str] = None

class AdminMatchCreatePayload(BaseModel):
    tournament_id: str
    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    team_a_placeholder: Optional[str] = None
    team_b_placeholder: Optional[str] = None
    round_name: Optional[str] = "Round 1"
    round_number: Optional[int] = 1
    match_number: Optional[int] = 1
    match_type: Optional[str] = "MATCH"
    stage: Optional[str] = None
    series_format: str = "BO1"
    match_format: Optional[str] = None
    team_ids: Optional[List[str]] = None
    teams: Optional[List[Dict[str, Any]]] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None
    scheduled_at: Optional[str] = None
    status: Optional[str] = "SCHEDULED"
    roster_status: Optional[str] = "PUBLISHED"  # "DRAFT", "PUBLISHED"
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    winner_team_id: Optional[str] = None
    games: Optional[List[Dict[str, Any]]] = None
    youtube_live_url: Optional[str] = None
    next_match_id: Optional[str] = None
    next_match_slot: Optional[str] = None  # "A" or "B"
    advancement_label: Optional[str] = None
    join_info_enabled: Optional[bool] = None
    join_info: Optional[Dict[str, str]] = None
    match_message: Optional[str] = None

class AdminMatchUpdatePayload(BaseModel):
    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    team_a_placeholder: Optional[str] = None
    team_b_placeholder: Optional[str] = None
    round_name: Optional[str] = None
    round_number: Optional[int] = None
    match_number: Optional[int] = None
    match_type: Optional[str] = None
    stage: Optional[str] = None
    series_format: Optional[str] = None
    match_format: Optional[str] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None
    scheduled_at: Optional[str] = None
    status: Optional[str] = None
    roster_status: Optional[str] = None  # "DRAFT", "PUBLISHED", "UPDATED", "CANCELLED"
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    winner_team_id: Optional[str] = None
    games: Optional[List[Dict[str, Any]]] = None
    youtube_live_url: Optional[str] = None
    live_link: Optional[str] = None
    next_match_id: Optional[str] = None
    next_match_slot: Optional[str] = None  # "A" or "B"
    advancement_label: Optional[str] = None
    join_info_enabled: Optional[bool] = None
    join_info: Optional[Dict[str, str]] = None
    match_message: Optional[str] = None

class AdminPublishRosterPayload(BaseModel):
    notify_leaders: bool = True
    match_notes: Optional[str] = None

class AdminMatchSetupItem(BaseModel):
    id: Optional[str] = None
    round_number: int = 1
    match_number: int = 1
    stage: str
    round_name: Optional[str] = None
    match_type: str = "QUALIFIER"
    format: Optional[str] = "Knockout"
    series_format: str = "BO1"
    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    team_a_placeholder: Optional[str] = None
    team_b_placeholder: Optional[str] = None
    team_ids: Optional[List[str]] = None
    teams: Optional[List[Dict[str, Any]]] = None
    roster_status: Optional[str] = "DRAFT"
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None
    scheduled_at: Optional[str] = None
    youtube_live_url: Optional[str] = None
    next_match_id: Optional[str] = None
    next_match_slot: Optional[str] = None
    advancement_label: Optional[str] = None
    join_info_enabled: Optional[bool] = None
    join_info: Optional[Dict[str, str]] = None
    match_message: Optional[str] = None

class AdminSaveLiveStructurePayload(BaseModel):
    match_format: str
    series_format: str = "BO1"
    formation_status: Optional[str] = "DRAFT"
    rounds: Optional[List[Dict[str, Any]]] = None
    matches: List[AdminMatchSetupItem]

class AdminMarkMatchLivePayload(BaseModel):
    youtube_live_url: Optional[str] = None

class AdminCompleteMatchPayload(BaseModel):
    winner_team_id: Optional[str] = None
    winner_id: Optional[str] = None
    score_a: int = 0
    score_b: int = 0
    team1_score: Optional[int] = None
    team2_score: Optional[int] = None
    captain_notes: Optional[str] = None

class TournamentCommunityUpdatePayload(BaseModel):
    community_platform: Optional[str] = None  # "DISCORD" | "WHATSAPP" | None
    community_link: Optional[str] = None

class MatchJoinInfoUpdatePayload(BaseModel):
    join_info_enabled: bool = False
    join_info: Optional[Dict[str, str]] = None  # e.g. {"party_id": "...", "party_code": "..."}
    match_message: Optional[str] = None

class MatchReminderRequest(BaseModel):
    reminder_type: str = "FIFTEEN_MINUTES"  # ONE_WEEK, TWO_DAYS, ONE_DAY, FIFTEEN_MINUTES, TEN_MINUTES, STARTING_NOW, STARTED, CUSTOM
    custom_message: Optional[str] = None

class CreateRoundPayload(BaseModel):
    round_name: str
    series_format: Optional[str] = "BO1"

class AdminMatchScheduleUpdate(BaseModel):
    scheduled_at: Optional[str] = None
    scheduled_time: Optional[str] = None
    live_link: Optional[str] = None
    youtube_live_url: Optional[str] = None
    match_format: Optional[str] = None
    series_format: Optional[str] = None
    match_type: Optional[str] = None
    stage: Optional[str] = None

class AdminMatchStatusUpdate(BaseModel):
    status: str  # SCHEDULED, CHECK_IN_OPEN, LIVE, COMPLETED, CANCELLED
    youtube_live_url: Optional[str] = None

class AdminMatchScoreUpdate(BaseModel):
    score_a: int
    score_b: int
    winner_team_id: Optional[str] = None
    complete_match: bool = False

class MatchResultSubmit(BaseModel):
    score_a: int
    score_b: int
    evidence_url: Optional[str] = None
    captain_notes: Optional[str] = None

# Dispute
class DisputeCreate(BaseModel):
    reason: str
    description: str
    evidence_url: Optional[str] = None

class OrderCreateRequest(BaseModel):
    tournament_id: str
    team_id: Optional[str] = None

class PaymentVerifyRequest(BaseModel):
    order_id: Optional[str] = None
    payment_id: Optional[str] = None
    signature: Optional[str] = None
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    razorpay_signature: Optional[str] = None

    def get_order_id(self) -> str:
        return self.order_id or self.razorpay_order_id or ""

    def get_payment_id(self) -> str:
        return self.payment_id or self.razorpay_payment_id or ""

    def get_signature(self) -> str:
        return self.signature or self.razorpay_signature or ""

# Admin Assignments & Disputes
class AdminAssignmentCreate(BaseModel):
    admin_user_id: str
    tournament_id: Optional[str] = None

class DisputeResolve(BaseModel):
    decision: str = "RESOLVED"
    resolution_notes: Optional[str] = None

class DisputeResponse(BaseModel):
    id: str
    tournament_id: Optional[str] = None
    match_id: Optional[str] = None
    submitted_by: Optional[str] = None
    raised_by: Optional[str] = None
    reason: str
    description: Optional[str] = None
    evidence_url: Optional[str] = None
    status: str
    resolution_notes: Optional[str] = None
    created_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True, extra="allow")

class LeaderboardEntryResponse(BaseModel):
    id: str
    name: str
    tag: str
    type: str = "SQUAD"
    game_slug: Optional[str] = None
    game_name: Optional[str] = None
    captain: Optional[str] = None
    logo_url: Optional[str] = None
    win_rate: Optional[Any] = 0.0
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    rating: int = 1500
    earnings: Optional[Any] = 0
    spending: Optional[int] = 0
    form: List[str] = []
    is_verified: bool = False
    category: str = "overall"
    rank: int = 1
    created_at: Optional[Any] = None
    is_disbanded: Optional[bool] = False
    is_disqualified: Optional[bool] = False
    status: Optional[str] = "ACTIVE"
    model_config = ConfigDict(from_attributes=True, extra="allow")

# Stage & Round Schemas
class TournamentStageCreate(BaseModel):
    name: str
    format: str = "KNOCKOUT"  # KNOCKOUT, ROUND_ROBIN, DOUBLE_ROUND_ROBIN, DOUBLE_ELIMINATION, SWISS, GROUP_STAGE
    series_format: str = "BO1"  # "BO1" or "BO3"
    advancing_teams_count: Optional[int] = None

class TournamentStageUpdate(BaseModel):
    name: Optional[str] = None
    format: Optional[str] = None
    series_format: Optional[str] = None
    advancing_teams_count: Optional[int] = None
    status: Optional[str] = None

class StageReorderPayload(BaseModel):
    stage_ids: List[str]

class StageRoundCreate(BaseModel):
    name: str
    round_number: Optional[int] = None
    format: Optional[str] = "SINGLE_ELIMINATION"
    series_format: Optional[str] = "BO1"
    match_count: Optional[int] = 1
    duration_minutes: Optional[int] = None
    advancement_rule: Optional[str] = None
    rules_notes: Optional[str] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None

class StageRoundUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    format: Optional[str] = None
    series_format: Optional[str] = None
    match_count: Optional[int] = None
    duration_minutes: Optional[int] = None
    advancement_rule: Optional[str] = None
    rules_notes: Optional[str] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None

class StageMatchCreate(BaseModel):
    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    series_format: str = "BO1"  # "BO1" or "BO3"
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None
    youtube_live_url: Optional[str] = None
    match_number: Optional[int] = None

class MatchAdvancementPayload(BaseModel):
    winner_team_id: Optional[str] = None
    destination_stage_id: str
    destination_round_id: str
    destination_match_id: str
    destination_slot: str = "team_a"  # "team_a" or "team_b"

class MatchSwapPayload(BaseModel):
    reason: Optional[str] = None

class TournamentStreamUpdatePayload(BaseModel):
    live_stream_url: str

class WalletAuditSummaryResponse(BaseModel):
    total_wallet_pool_inr: float
    total_money_added_inr: float
    total_money_spent_inr: float
    total_transactions: int
    transactions: List[Dict[str, Any]]

# Genre Schemas
class GenreResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str = ""
    banner_url: Optional[str] = None
    icon_url: Optional[str] = None
    rotation_interval_seconds: int = 4
    display_order: int = 0
    games: List[GameResponse] = []
    games_count: int = 0
    game_count: int = 0
    is_active: bool = True
    model_config = ConfigDict(from_attributes=True)

class GenreCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = ""
    banner_url: Optional[str] = None
    icon_url: Optional[str] = None
    rotation_interval_seconds: Optional[int] = 4
    display_order: Optional[int] = 0
    is_active: Optional[bool] = True

class GenreUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    banner_url: Optional[str] = None
    icon_url: Optional[str] = None
    rotation_interval_seconds: Optional[int] = None
    display_order: Optional[int] = None
    is_active: Optional[bool] = None

# Review Schemas
class ReviewResponse(BaseModel):
    id: str
    user_id: str
    user_name: str
    avatar_url: Optional[str] = None
    rating: int
    review_text: str
    category: str = "PLATFORM"
    status: str = "PENDING"
    is_verified: bool = True
    is_featured: bool = False
    admin_notes: Optional[str] = None
    created_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class ReviewCreate(BaseModel):
    rating: int = Field(default=5, ge=1, le=5)
    review_text: str
    category: Optional[str] = "PLATFORM"
    user_name: Optional[str] = None
    avatar_url: Optional[str] = None

class ReviewStatusUpdate(BaseModel):
    status: str  # APPROVED, FEATURED, HIDDEN, PENDING
    admin_notes: Optional[str] = None

class ReviewSummaryResponse(BaseModel):
    average_rating: float
    total_reviews: int
    rating_distribution: Dict[str, int]
    reviews: List[ReviewResponse]
    featured_review: Optional[ReviewResponse] = None


