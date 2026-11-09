from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text

from tenant_hooks.api import events, intake
from tenant_hooks.config import Settings
from tenant_hooks.db import make_engine
from tenant_hooks.security.secret_box import SecretBox


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()  # type: ignore[call-arg]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.secret_box = SecretBox(resolved.secret_key)
        app.state.engine = make_engine(resolved.database_url)
        yield
        app.state.engine.dispose()

    app = FastAPI(title="tenant-hooks", lifespan=lifespan)
    app.include_router(intake.router)
    app.include_router(events.router)

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, str]:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app
