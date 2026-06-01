"""Small dependency-free utilities: retry/backoff, rate limiting, progress.

Uses tenacity/tqdm when installed, otherwise pure-stdlib fallbacks so the
scraper runs in minimal environments.
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterable

log = logging.getLogger("e14")


class RetryableError(Exception):
    """Raised by callers to signal a transient failure worth retrying."""


def retry(
    attempts: int = 5,
    base: float = 1.0,
    cap: float = 30.0,
    exceptions: tuple[type[BaseException], ...] = (RetryableError,),
):
    """Decorator: exponential backoff with jitter, capped attempts.

    Deterministic jitter (no Math.random dependency): jitter grows with the
    attempt index but stays bounded, good enough to de-synchronize workers.
    """

    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last = exc
                    if i == attempts - 1:
                        break
                    delay = min(cap, base * (2**i))
                    # bounded pseudo-jitter from time fraction
                    jitter = (time.monotonic() % 1.0) * base
                    sleep_for = delay + jitter
                    log.debug(
                        "retry %s/%s after %.1fs: %s",
                        i + 1, attempts, sleep_for, exc,
                    )
                    time.sleep(sleep_for)
            raise last  # type: ignore[misc]

        return wrapper

    return deco


class RateLimiter:
    """Thread-safe global rate limiter (token-bucket-ish, min interval)."""

    def __init__(self, rate_per_sec: float):
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._min_interval


def progress(iterable: Iterable, total: int | None = None, desc: str = ""):
    """tqdm if available, else a lightweight stderr counter."""
    try:
        from tqdm import tqdm

        return tqdm(iterable, total=total, desc=desc, unit="acta")
    except Exception:  # pragma: no cover - fallback
        return _PlainProgress(iterable, total, desc)


class _PlainProgress:
    def __init__(self, iterable, total, desc):
        self._it = iter(iterable)
        self._total = total
        self._desc = desc
        self._n = 0
        self._last = 0.0

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self._it)
        self._n += 1
        now = time.monotonic()
        if now - self._last > 2.0:
            self._last = now
            tail = f"/{self._total}" if self._total else ""
            print(f"\r{self._desc}: {self._n}{tail}", end="", flush=True)
        return item

    # tqdm-compatible no-ops used by callers
    def set_postfix(self, *a, **k):
        pass

    def write(self, msg):
        print(msg)
