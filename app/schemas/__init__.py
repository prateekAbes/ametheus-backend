from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, ConfigDict

# User
class UserBase(BaseModel):
    email: str
    username: str
    role: str = "PLAYER"
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
    title: str
    game_slug: str
    game_name: str
    prize_pool: str
    prize_pool_inr: int
    entry_fee: str
    entry_fee_inr: int
    status: str
    starts_at: str
    roster_lock_time: str
    server_region: str
    registered_teams: int
    max_teams: int
    rank_requirement: str
    format: str
    banner_url: str
    badge_color: str
    description: str
    rules: List[str] = []
    schedule: List[Dict[str, Any]] = []
    prizes: List[Dict[str, Any]] = []
    bracket_rounds: List[Dict[str, Any]] = []
    registered_rosters: List[Dict[str, Any]] = []
    model_config = ConfigDict(from_attributes=True)

class TournamentRegisterRequest(BaseModel):
    team_id: str

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
