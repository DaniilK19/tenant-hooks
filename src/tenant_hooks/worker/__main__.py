import logging
import signal
import threading
from types import FrameType

from tenant_hooks.config import WorkerSettings
from tenant_hooks.db import make_engine
from tenant_hooks.handlers.demo import build_registry
from tenant_hooks.worker.backoff import RetryPolicy
from tenant_hooks.worker.loop import Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = WorkerSettings()  # type: ignore[call-arg]
    engine = make_engine(settings.database_url, pool_size=2)
    worker = Worker(
        engine,
        build_registry(),
        lease_seconds=settings.worker_lease_seconds,
        batch_size=settings.worker_batch_size,
        retry=RetryPolicy(settings.retry_base_seconds, settings.retry_cap_seconds),
    )

    stop = threading.Event()

    def request_stop(_signal: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logging.getLogger(__name__).info("worker %s started", worker.worker_id)
    worker.run_forever(stop, settings.worker_poll_seconds, settings.purge_interval_seconds)
    engine.dispose()


if __name__ == "__main__":
    main()
