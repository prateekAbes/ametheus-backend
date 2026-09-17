"""add_audit_log_hashes

Revision ID: 2522165c0304
Revises: 9d2b1e9d9c91
Create Date: 2026-09-17 13:35:49.026118

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2522165c0304'
down_revision: Union[str, Sequence[str], None] = '9d2b1e9d9c91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    from sqlalchemy import inspect
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "audit_logs" in tables:
        columns = {c['name'] for c in inspector.get_columns('audit_logs')}
        if 'prev_hash' not in columns:
            op.add_column('audit_logs', sa.Column('prev_hash', sa.String(length=64), nullable=True))
        if 'record_hash' not in columns:
            op.add_column('audit_logs', sa.Column('record_hash', sa.String(length=64), nullable=True))

    if "tournaments" in tables:
        t_cols = {c['name'] for c in inspector.get_columns('tournaments')}
        if 'organizer_id' not in t_cols:
            op.add_column('tournaments', sa.Column('organizer_id', sa.String(length=64), nullable=True))

    if "tournament_registrations" in tables:
        reg_cols = {c['name'] for c in inspector.get_columns('tournament_registrations')}
        if 'order_id' not in reg_cols:
            op.add_column('tournament_registrations', sa.Column('order_id', sa.String(length=64), nullable=True))
        if 'payment_status' not in reg_cols:
            op.add_column('tournament_registrations', sa.Column('payment_status', sa.String(length=32), server_default="FREE", nullable=False))
        if 'check_in_status' not in reg_cols:
            op.add_column('tournament_registrations', sa.Column('check_in_status', sa.String(length=32), server_default="PENDING", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('audit_logs', 'record_hash')
    op.drop_column('audit_logs', 'prev_hash')
