from sqlalchemy import Column, String, ForeignKey, Text, BigInteger
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

class TournamentAdminAssignment(Base, TimestampMixin):
    __tablename__ = "tournament_admin_assignments"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    admin_user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tournament_id = Column(String(36), ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False)
    assigned_by = Column(String(64), ForeignKey("users.id"), nullable=False)

    # Relationships
    tournament = relationship("Tournament", back_populates="admin_assignments")

class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    operator = Column(String(64), nullable=False)
    role = Column(String(32), nullable=False)
    action = Column(String(64), nullable=False)
    target = Column(String(128), nullable=False)
    details = Column(Text, nullable=True)
    ip_address = Column(String(64), default="127.0.0.1")
    prev_hash = Column(String(64), nullable=True)
    record_hash = Column(String(64), nullable=True)
