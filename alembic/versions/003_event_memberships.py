"""Replace global User.role with per-event memberships.

- Drop User.role column, add User.is_admin boolean
- Create event_memberships table (user_id, event_id, role)

Revision ID: 003
Revises: 002
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create event_memberships table
    op.create_table(
        'event_memberships',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_membership_user_event', 'event_memberships', ['user_id', 'event_id'], unique=True)

    # Add is_admin to users
    op.add_column('users', sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.text('0')))

    # Drop role from users (SQLite doesn't support DROP COLUMN before 3.35)
    # Use batch mode for SQLite compatibility
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('role')


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(20), nullable=False, server_default='listener'))
        batch_op.drop_column('is_admin')
    op.drop_index('ix_membership_user_event', table_name='event_memberships')
    op.drop_table('event_memberships')
