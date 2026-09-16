from sqlalchemy import Column, String
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin

class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True)  # Clerk ID e.g. user_xxx or custom
    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(64), unique=True, index=True, nullable=False)
    avatar_url = Column(String(512), nullable=True)
    role = Column(String(32), nullable=False, default="PLAYER")  # PLAYER, TOURNAMENT_ADMIN, SUPER_ADMIN
    riot_id = Column(String(64), nullable=True)
    steam_id = Column(String(64), nullable=True)
    inr_balance = Column(Integer, default=0, nullable=False) if False else Column(String(32), default="0")

    # Relationships
    teams_created = relationship("Team", back_populates="captain")
    team_memberships = relationship("TeamMember", back_populates="user")
