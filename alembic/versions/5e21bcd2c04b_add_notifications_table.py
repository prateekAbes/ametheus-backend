"""add_notifications_table

Revision ID: 5e21bcd2c04b
Revises: 2522165c0304
Create Date: 2026-09-17 14:39:32.282224

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e21bcd2c04b'
down_revision: Union[str, Sequence[str], None] = '2522165c0304'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    from sqlalchemy import inspect
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "notifications" not in tables:
        op.create_table(
            'notifications',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('user_id', sa.String(length=64), nullable=False),
            sa.Column('type', sa.String(length=32), nullable=False, server_default='SYSTEM'),
            sa.Column('title', sa.String(length=128), nullable=False),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('is_read', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('link', sa.String(length=256), nullable=True),
            sa.Column('metadata_json', sa.Text(), nullable=False, server_default='{}'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_notifications_user_id'), 'notifications', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    from sqlalchemy import inspect
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "notifications" in tables:
        op.drop_index(op.f('ix_notifications_user_id'), table_name='notifications')
        op.drop_table('notifications')
