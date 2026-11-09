"""A real worker process that the crash test kills in the middle of a handler.

Run as: python -m tests.processing.crash_worker <database url> <directory> <lease seconds>
"""

import sys
import time
from pathlib import Path

from tenant_hooks.db import make_engine
from tenant_hooks.handlers.registry import HandlerContext, HandlerRegistry
from tenant_hooks.worker.loop import Worker
from tests.processing.file_effects import FileEffects

HANDLER_NAME = "test_handler"
WORKER_ID = "doomed"
# Only a safety net: the parent test kills this process long before this runs out.
HANG_SECONDS = 120


def main() -> None:
    database_url, directory, lease_seconds = sys.argv[1:4]
    effects = FileEffects(Path(directory))

    def apply_then_hang(context: HandlerContext) -> None:
        effects.apply(context.idempotency_key)
        effects.mark_started(context.attempt)
        time.sleep(HANG_SECONDS)

    registry = HandlerRegistry()
    registry.register(HANDLER_NAME, apply_then_hang)
    engine = make_engine(database_url, pool_size=2)
    Worker(engine, registry, worker_id=WORKER_ID, lease_seconds=int(lease_seconds)).run_once()


if __name__ == "__main__":
    main()
