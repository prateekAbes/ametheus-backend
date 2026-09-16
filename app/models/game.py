from sqlalchemy import Column, String, Boolean, Integer, Text
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

class Game(Base, TimestampMixin):
    __tablename__ = "games"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    slug = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(128), nullable=False)
    category = Column(String(32), nullable=False, default="pc")  # pc, mobile, crossplay, solo
    platform = Column(String(64), nullable=False)
    tag = Column(String(64), nullable=False)
    is_team_game = Column(Boolean, nullable=False, default=True)
    min_team_size = Column(Integer, nullable=False, default=1)
    max_team_size = Column(Integer, nullable=False, default=5)
    banner_url = Column(String(512), nullable=False)
    logo_url = Column(String(512), nullable=False)
    description = Column(Text, nullable=False)
    server_region = Column(String(128), nullable=False, default="AWS Mumbai (ap-south-1)")
    anti_cheat = Column(String(128), nullable=False, default="Hardware TPM 2.0 Vanguard")
    match_protocol = Column(String(128), nullable=False, default="Competitive MR12")
    accent_color = Column(String(32), default="#E56A10")

    # Relationships
    tournaments = relationship("Tournament", back_populates="game")
    teams = relationship("Team", back_populates="game")
