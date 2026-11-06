"""Reusable bounded provider retries, circuit breaker and timestamp-preserving TTL cache."""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError


@dataclass
class CircuitBreaker:
    threshold: int = 3
    recovery_seconds: float = 30
    failures: int = 0
    opened_at: float | None = None

    def before_call(self):
        if self.opened_at is not None and time.monotonic() - self.opened_at < self.recovery_seconds:
            raise BetterError(ErrorCode.TRANSIENT_PROVIDER_ERROR, "Provider circuit open")

    def success(self):
        self.failures, self.opened_at = 0, None

    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class ProviderExecutor:
    def __init__(
        self,
        timeout: float = 5,
        retries: int = 1,
        rate_per_second: float = 10,
        breaker: CircuitBreaker | None = None,
    ):
        if timeout <= 0 or retries < 0 or rate_per_second <= 0:
            raise ValueError("invalid provider execution policy")
        self.timeout, self.retries, self.interval = timeout, retries, 1 / rate_per_second
        self.breaker = breaker or CircuitBreaker()
        self._lock, self._last = asyncio.Lock(), 0.0

    async def call(self, operation: Callable[[], Awaitable[Any]]):
        for attempt in range(self.retries + 1):
            self.breaker.before_call()
            async with self._lock:
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - self._last)))
                self._last = time.monotonic()
            try:
                result = await asyncio.wait_for(operation(), self.timeout)
            except TimeoutError:
                error = BetterError(ErrorCode.TIMEOUT, "Provider request timed out")
            except BetterError as exc:
                error = exc
            else:
                self.breaker.success()
                return result
            self.breaker.failure()
            if not error.detail.retryable or attempt == self.retries:
                raise error
            await asyncio.sleep(0.05 * 2**attempt)


@dataclass(frozen=True)
class CacheEntry:
    payload: Any
    fetched_at: datetime
    expires_at: datetime
    provider: str


class SnapshotCache:
    def __init__(self):
        self._entries: dict[str, CacheEntry] = {}

    def put(self, key: str, entry: CacheEntry):
        if entry.fetched_at.tzinfo is None or entry.expires_at <= entry.fetched_at:
            raise ValueError("cache requires aware timestamps and positive TTL")
        self._entries[key] = entry

    def get(self, key: str, as_of: datetime) -> CacheEntry | None:
        entry = self._entries.get(key)
        return entry if entry and entry.fetched_at <= as_of <= entry.expires_at else None
