import random
from collections.abc import Callable
from dataclasses import dataclass

Rng = Callable[[float, float], float]


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 5.0
    cap_seconds: float = 900.0
    rng: Rng = random.uniform

    def delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter. `attempt` is the one that just failed (1-based)."""
        ceiling = min(self.cap_seconds, self.base_seconds * 2 ** (attempt - 1))
        return self.rng(0, ceiling)
