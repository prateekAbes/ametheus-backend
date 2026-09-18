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
    banner_url: str
    logo_url: str
    description: str
    server_region: str
    anti_cheat: str
    match_protocol: str
    accent_color: Optional[str] = "#E56A10"
    active_tournaments_count: int = 0
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
    banner_url: str = "https://images.unsplash.com/photo-1542751371-adc38448a05e"
    logo_url: str = "https://images.unsplash.com/photo-1542751371-adc38448a05e"
    description: str = ""
    server_region: str = "AWS Mumbai (ap-south-1)"
    anti_cheat: str = "Standard Protocol"
    match_protocol: str = "Custom Lobby"
    accent_color: Optional[str] = "#E56A10"

class GameUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    platform: Optional[str] = None
    tag: Optional[str] = None
    is_team_game: Optional[bool] = None
    min_team_size: Optional[int] = None
    max_team_size: Optional[int] = None
    banner_url: Optional[str] = None
    logo_url: Optional[str] = None
    description: Optional[str] = None
    server_region: Optional[str] = None
    anti_cheat: Optional[str] = None
    match_protocol: Optional[str] = None
    accent_color: Optional[str] = None

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

class TeamResponse(BaseModel):
    id: str
    game_slug: str
    name: str
    tag: str
    invite_code: str
    captain_id: str
    captain_username: str
    max_members: int
    members: List[TeamMemberResponse] = []
    model_config = ConfigDict(from_attributes=True)

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
    starts_at: Optional[str] = "TBD"
    roster_lock_time: Optional[str] = "TBD"
    server_region: Optional[str] = "AWS Mumbai (ap-south-1)"
    registered_teams: Optional[int] = 0
    max_teams: Optional[int] = 16
    min_team_size: Optional[int] = 1
    max_team_size: Optional[int] = 5
    rank_requirement: Optional[str] = "Open to All Ranks"
    format: Optional[str] = "Single Elimination Bracket"
    banner_url: Optional[str] = "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop"
    badge_color: Optional[str] = "#E56A10"
    description: Optional[str] = ""
    rules: List[str] = []
    schedule: List[Dict[str, Any]] = []
    prizes: List[Dict[str, Any]] = []
    bracket_rounds: List[Dict[str, Any]] = []
    registered_rosters: List[Dict[str, Any]] = []
    model_config = ConfigDict(from_attributes=True)

class TournamentRegisterRequest(BaseModel):
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    player_handle: Optional[str] = None

# Match
class MatchResponse(BaseModel):
    id: str
    tournament_id: str
    tournament_title: str
    game_slug: str
    game_name: str
    stage: str
    format: str
    scheduled_time: str
    status: str
    server_region: str
    lobby_code: str
    lobby_password: str
    team_a: Optional[Dict[str, Any]] = None
    team_b: Optional[Dict[str, Any]] = None
    evidence_url: Optional[str] = None
    dispute_notes: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class MatchResultSubmit(BaseModel):
    score_a: int
    score_b: int
    evidence_url: Optional[str] = "https://r2.ametheus.gg/proof.png"
    captain_notes: Optional[str] = None

# Dispute
class DisputeCreate(BaseModel):
    reason: str
    description: str
    evidence_url: Optional[str] = None

class DisputeResolve(BaseModel):
    resolution_notes: str
    decision: str = "RESOLVED"  # RESOLVED or CLOSED

class DisputeResponse(BaseModel):
    id: str
    tournament_id: str
    match_id: str
    submitted_by: str
    reason: str
    description: str
    evidence_url: Optional[str] = None
    status: str
    created_at: str
    model_config = ConfigDict(from_attributes=True)

# Leaderboard
class LeaderboardEntryResponse(BaseModel):
    rank: int
    id: str
    name: str
    tag: str
    type: str
    game_slug: str
    game_name: str
    captain: str
    logo_url: str
    win_rate: float
    matches_played: int
    wins: int
    losses: int
    rating: int
    earnings: int
    form: List[str]
    is_verified: bool
    category: str

# Admin
class AdminAssignmentCreate(BaseModel):
    admin_user_id: str
    tournament_id: str

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
