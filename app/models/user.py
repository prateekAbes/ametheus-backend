from sqlalchemy import Column, String
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin

class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True)  # Application User ID or Clerk ID
    clerk_user_id = Column(String(128), unique=True, index=True, nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(64), unique=True, index=True, nullable=False)
    display_name = Column(String(128), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    role = Column(String(32), nullable=False, default="PLAYER")  # PLAYER, ORGANIZER, SUPER_ADMIN
    riot_id = Column(String(64), nullable=True)
    steam_id = Column(String(64), nullable=True)
    inr_balance = Column(String(32), default="0")

    # Relationships
    teams_created = relationship("Team", back_populates="captain")
    team_memberships = relationship("TeamMember", back_populates="user")
