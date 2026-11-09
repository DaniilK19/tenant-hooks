from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text

from tenant_hooks.config import Settings
from tenant_hooks.db import make_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.engine = make_engine(Settings().database_url)  # type: ignore[call-arg]
    yield
    app.state.engine.dispose()


app = FastAPI(title="tenant-hooks", lifespan=lifespan)


@app.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    with request.app.state.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}
