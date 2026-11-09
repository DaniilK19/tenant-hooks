"""A side effect that outlives a killed process. Imported by the crash worker, so it stays light."""

from pathlib import Path


class FileEffects:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def mark_started(self, attempt: int) -> None:
        (self.directory / f"started-{attempt}").touch()

    def has_started(self, attempt: int) -> bool:
        return (self.directory / f"started-{attempt}").exists()

    def apply(self, idempotency_key: str) -> bool:
        """Apply the effect once per key. Returns False if it had already been applied."""
        try:
            self.path(idempotency_key).touch(exist_ok=False)
        except FileExistsError:
            return False
        return True

    def path(self, idempotency_key: str) -> Path:
        return self.directory / f"effect-{idempotency_key}"
