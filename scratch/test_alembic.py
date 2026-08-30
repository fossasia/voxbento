def upgrade() -> None:
    import sqlalchemy as sa

    from alembic import op
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("oauth_audit_logs") as batch_op:
            batch_op.alter_column("client_id", existing_type=sa.INTEGER(), nullable=True)
            batch_op.drop_constraint("oauth_audit_logs_client_id_fkey", type_="foreignkey")
            batch_op.create_foreign_key(None, "oauth_clients", ["client_id"], ["id"], ondelete="SET NULL")
    else:
        pass
