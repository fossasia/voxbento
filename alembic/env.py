"""Alembic environment — async-aware for SQLAlchemy 2.0 + aiosqlite.

Reads DATABASE_URL from the environment (via portal.config.settings) so
that Alembic and the application always use the same database.  Falls back
to the ``sqlalchemy.url`` value in ``alembic.ini`` if the env var is unset.

Revision IDs are sequential three-digit numbers (001, 002, …) instead of
random hex strings, making the migration history easy to read.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from portal.config import settings
from portal.models import Base

config = context.config

# Override alembic.ini URL with the application's DATABASE_URL when set via env.
if settings.database_url:
    config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Sequential revision IDs (001, 002, …)
# ---------------------------------------------------------------------------


def _next_rev_id() -> str:
    """Return the next zero-padded revision number based on existing files."""
    versions_dir = os.path.join(os.path.dirname(__file__), "versions")
    max_num = 0
    if os.path.isdir(versions_dir):
        for name in os.listdir(versions_dir):
            if name.endswith(".py") and not name.startswith("__"):
                try:
                    num = int(name.split("_", 1)[0])
                    max_num = max(max_num, num)
                except ValueError:
                    pass
    return f"{max_num + 1:03d}"


def process_revision_directives(context, revision, directives):
    """Replace the random hex revision ID with a sequential number."""
    if directives:
        script = directives[0]
        script.rev_id = _next_rev_id()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        process_revision_directives=process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
