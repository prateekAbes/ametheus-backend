import json
from sqlalchemy import Column, String, Integer, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

class Tournament(Base, TimestampMixin):
    __tablename__ = "tournaments"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    slug = Column(String(128), unique=True, index=True, nullable=False)
    game_id = Column(String(36), ForeignKey("games.id"), nullable=False)
    title = Column(String(255), nullable=False)
    prize_pool_inr = Column(Integer, default=0, nullable=False)
    entry_fee_inr = Column(Integer, default=0, nullable=False)
    status = Column(String(32), default="REGISTRATION_OPEN", nullable=False)  # REGISTRATION_OPEN, CHECK_IN, LIVE, COMPLETED
    starts_at = Column(String(64), nullable=False)
    roster_lock_time = Column(String(64), nullable=False)
    server_region = Column(String(128), default="AWS Mumbai (ap-south-1)")
    max_teams = Column(Integer, default=16, nullable=False)
    rank_requirement = Column(String(64), default="Open to All Ranks")
    format = Column(String(128), default="Single Elimination Bracket")
    banner_url = Column(String(512), nullable=False)
    badge_color = Column(String(32), default="#E56A10")
    description = Column(Text, nullable=False)
    rules_json = Column(Text, nullable=False, default="[]")
    schedule_json = Column(Text, nullable=False, default="[]")
    prizes_json = Column(Text, nullable=False, default="[]")
    bracket_json = Column(Text, nullable=False, default="[]")

    # Relationships
    game = relationship("Game", back_populates="tournaments")
    registrations = relationship("TournamentRegistration", back_populates="tournament")
    matches = relationship("Match", back_populates="tournament")
    admin_assignments = relationship("TournamentAdminAssignment", back_populates="tournament")

class TournamentRegistration(Base, TimestampMixin):
    __tablename__ = "tournament_registrations"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tournament_id = Column(String(36), ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False)
    team_id = Column(String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    payment_status = Column(String(32), default="FREE", nullable=False)  # FREE, PENDING, PAID, REFUNDED
    order_id = Column(String(64), nullable=True)
    check_in_status = Column(String(32), default="PENDING", nullable=False)  # PENDING, CHECKED_IN

    # Relationships
    tournament = relationship("Tournament", back_populates="registrations")
    team = relationship("Team", back_populates="registrations")
