"""Alembic environment. Migrations always run as the hooks_owner role."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from tenant_hooks.models import Base

engine = create_engine(os.environ["HOOKS_MIGRATION_DATABASE_URL"], poolclass=pool.NullPool)

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
