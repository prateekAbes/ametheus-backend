import secrets
import string
from sqlalchemy import Column, String, Integer, ForeignKey
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

def generate_invite_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace("0", "").replace("O", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(chars) for _ in range(length))

class Team(Base, TimestampMixin):
    __tablename__ = "teams"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    game_id = Column(String(36), ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    captain_id = Column(String(64), ForeignKey("users.id"), nullable=False)
    name = Column(String(128), nullable=False)
    tag = Column(String(10), nullable=False)
    logo_url = Column(String(512), nullable=True)
    invite_code = Column(String(6), unique=True, index=True, nullable=False, default=generate_invite_code)
    max_members = Column(Integer, default=5, nullable=False)

    # Relationships
    game = relationship("Game", back_populates="teams")
    captain = relationship("User", back_populates="teams_created")
    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    registrations = relationship("TournamentRegistration", back_populates="team")

class TeamMember(Base, TimestampMixin):
    __tablename__ = "team_members"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    team_id = Column(String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(32), default="MEMBER", nullable=False)  # CAPTAIN, MEMBER
    game_handle = Column(String(64), nullable=False)

    # Relationships
    team = relationship("Team", back_populates="members")
    user = relationship("User", back_populates="team_memberships")
