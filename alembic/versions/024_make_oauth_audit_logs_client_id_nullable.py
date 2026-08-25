"""make oauth_audit_logs client_id nullable

Revision ID: 024
Revises: 023
Create Date: 2026-08-26 00:05:29.080992

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "024"
down_revision: Union[str, Sequence[str], None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        naming_convention = {
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
        with op.batch_alter_table("oauth_audit_logs", naming_convention=naming_convention) as batch_op:
            batch_op.alter_column("client_id", existing_type=sa.INTEGER(), nullable=True)
            batch_op.drop_constraint("fk_oauth_audit_logs_client_id_oauth_clients", type_="foreignkey")
            batch_op.create_foreign_key(
                "fk_oauth_audit_logs_client_id_oauth_clients",
                "oauth_clients",
                ["client_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        op.alter_column("oauth_audit_logs", "client_id", existing_type=sa.INTEGER(), nullable=True)
        op.drop_constraint("oauth_audit_logs_client_id_fkey", "oauth_audit_logs", type_="foreignkey")
        op.create_foreign_key(None, "oauth_audit_logs", "oauth_clients", ["client_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        naming_convention = {
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
        with op.batch_alter_table("oauth_audit_logs", naming_convention=naming_convention) as batch_op:
            batch_op.drop_constraint("fk_oauth_audit_logs_client_id_oauth_clients", type_="foreignkey")
            batch_op.create_foreign_key(
                "fk_oauth_audit_logs_client_id_oauth_clients",
                "oauth_clients",
                ["client_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch_op.alter_column("client_id", existing_type=sa.INTEGER(), nullable=False)
    else:
        op.drop_constraint("oauth_audit_logs_client_id_fkey", "oauth_audit_logs", type_="foreignkey")
        op.create_foreign_key(None, "oauth_audit_logs", "oauth_clients", ["client_id"], ["id"], ondelete="CASCADE")
        op.alter_column("oauth_audit_logs", "client_id", existing_type=sa.INTEGER(), nullable=False)
