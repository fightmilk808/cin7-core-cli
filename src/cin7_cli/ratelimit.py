"""A polite, cross-process rate limiter.

Cin7 allows 60 calls a minute per API application, and a key is often shared
with other integrations. Every `cin7` process on this machine takes its
turn through one lock file, so a shell loop of single calls is paced the same as
one `--all-pages` run.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

DEFAULT_PER_MIN = 40
MAX_PER_MIN = 60


def state_dir() -> Path:
    """Where the shared pacing file lives."""
    base = os.environ.get("CIN7_CLI_STATE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "cin7-cli"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


class RateLimiter:
    """Spaces calls at least `60 / per_min` seconds apart across processes."""

    def __init__(self, per_min: float = DEFAULT_PER_MIN, path: Path | None = None, sleep=time.sleep, clock=time.time):
        if per_min <= 0:
            raise ValueError("rate must be positive")
        self.per_min = min(float(per_min), MAX_PER_MIN)
        self.interval = 60.0 / self.per_min
        self.path = path or state_dir() / "last-call"
        self._sleep = sleep
        self._clock = clock

    def wait(self) -> float:
        """Block until this process may make a call. Returns seconds slept."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()
                last = float(raw) if raw else 0.0
                now = self._clock()
                delay = max(0.0, last + self.interval - now)
                if delay:
                    self._sleep(delay)
                fh.seek(0)
                fh.truncate()
                fh.write(f"{self._clock():.3f}")
                fh.flush()
                return delay
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def penalise(self, seconds: float) -> None:
        """Push the shared next-call time out, e.g. after a 429."""
        with open(self.path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(f"{self._clock() + seconds - self.interval:.3f}")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
