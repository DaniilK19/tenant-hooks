from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session


def make_engine(url: str, pool_size: int = 10) -> Engine:
    # max_overflow=0 makes pool_size a hard limit on database connections.
    return create_engine(url, pool_size=pool_size, max_overflow=0, pool_pre_ping=True)


@contextmanager
def tenant_tx(engine: Engine, tenant_id: UUID) -> Iterator[Session]:
    """Open a transaction scoped to one tenant. Commits on success, rolls back on error.

    This is the only place in the application that sets the tenant. It uses
    set_config(..., is_local => true), which is SET LOCAL with a bind parameter:
    the value disappears when the transaction ends, so it cannot follow the
    connection back into the pool.
    """
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session
