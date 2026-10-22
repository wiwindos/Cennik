"""Small thread-safe phase measurements; nested phases have inclusive durations."""
import threading
from contextlib import contextmanager
from time import perf_counter


class Performance:
    def __init__(self):
        self.started = perf_counter()
        self.stopped: float | None = None
        self.stage: str | None = None
        self.timings: dict[str, float] = {}
        self.lock = threading.RLock()

    @contextmanager
    def measure(self, stage: str):
        start = perf_counter()
        with self.lock:
            previous, self.stage = self.stage, stage
        try:
            yield
        finally:
            with self.lock:
                self.timings[stage] = self.timings.get(stage, 0.0) + perf_counter() - start
                self.stage = previous

    def stop(self):
        with self.lock:
            self.stopped = perf_counter()
            self.stage = None

    def snapshot(self) -> dict:
        with self.lock:
            return {"stage": self.stage, "elapsed_seconds": round((self.stopped or perf_counter()) - self.started, 3),
                    "timings_seconds": {k: round(v, 3) for k, v in self.timings.items()},
                    "nested_timings_are_inclusive": True}
