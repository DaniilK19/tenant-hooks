FROM python:3.13-slim

RUN pip install --no-cache-dir uv==0.11.11

WORKDIR /app

# Dependencies first, so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 hooks
USER hooks

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "--factory", "tenant_hooks.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
