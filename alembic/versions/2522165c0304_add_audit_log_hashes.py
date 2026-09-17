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
    op.add_column('audit_logs', sa.Column('prev_hash', sa.String(length=64), nullable=True))
    op.add_column('audit_logs', sa.Column('record_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('audit_logs', 'record_hash')
    op.drop_column('audit_logs', 'prev_hash')
