from sqlalchemy import Column, String, Integer, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

class Match(Base, TimestampMixin):
    __tablename__ = "matches"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tournament_id = Column(String(36), ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False)
    stage = Column(String(64), nullable=False)  # e.g. "Quarterfinals — Match 1"
    round_number = Column(Integer, default=1, nullable=False)
    match_number = Column(Integer, default=1, nullable=False)
    format = Column(String(128), default="Best of 3")
    scheduled_time = Column(String(64), nullable=False)
    status = Column(String(32), default="CHECK_IN_OPEN", nullable=False)  # CHECK_IN_OPEN, LIVE, PENDING_VERIFICATION, DISPUTED, COMPLETED

    team_a_id = Column(String(36), ForeignKey("teams.id"), nullable=True)
    team_b_id = Column(String(36), ForeignKey("teams.id"), nullable=True)
    score_a = Column(Integer, nullable=True)
    score_b = Column(Integer, nullable=True)
    winner_team_id = Column(String(36), nullable=True)

    server_region = Column(String(128), default="AWS Mumbai (ap-south-1)")
    lobby_code = Column(String(64), nullable=False)
    lobby_password = Column(String(64), nullable=False)
    evidence_url = Column(String(512), nullable=True)
    captain_notes = Column(Text, nullable=True)
    dispute_notes = Column(Text, nullable=True)

    # Relationships
    tournament = relationship("Tournament", back_populates="matches")
    team_a = relationship("Team", foreign_keys=[team_a_id])
    team_b = relationship("Team", foreign_keys=[team_b_id])
