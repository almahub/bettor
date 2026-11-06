"""Real authenticated HTTP ingestion for a provider exposing Better's documented feed contract.

This adapter is opt-in. It does not pretend a particular commercial API uses these routes.
Commercial schemas require their own mapping adapter before they enter this boundary.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

import httpx
from pydantic import TypeAdapter, ValidationError

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.domain.models import LineupSnapshot, Match, NewsSnapshot, OddsSnapshot, StatsSnapshot

from .resilience import CacheEntry, ProviderExecutor, SnapshotCache


class HTTPFootballProvider:
    def __init__(
        self,
        base_url: str,
        provider_id: str,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 3,
        retries: int = 1,
        ttl_seconds: int = 60,
        rate_per_second: float = 5,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Provider requires an HTTP(S) URL without embedded credentials")
        if ttl_seconds <= 0 or not provider_id:
            raise ValueError("Provider requires positive cache TTL and source identity")
        self.base_url = base_url.rstrip("/")
        self.provider_id = provider_id
        self.client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.executor = ProviderExecutor(timeout, retries, rate_per_second)
        self.cache = SnapshotCache()
        self.ttl_seconds = ttl_seconds

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _fetch(self, path, schema, as_of=None):
        key = hashlib.sha256(f"{self.provider_id}:{path}:{as_of}".encode()).hexdigest()
        if as_of and (cached := self.cache.get(key, datetime.now(timezone.utc))):
            return (
                schema.validate_python(cached.payload)
                if isinstance(schema, TypeAdapter)
                else schema.model_validate(cached.payload)
            )

        async def request():
            try:
                response = await self.client.get(
                    self.base_url + path,
                    headers=self._headers,
                    params={"as_of": as_of.isoformat()} if as_of else None,
                )
            except httpx.TimeoutException as exc:
                raise BetterError(ErrorCode.TIMEOUT, "Feed timed out", source=self.provider_id) from exc
            except httpx.TransportError as exc:
                raise BetterError(
                    ErrorCode.TRANSIENT_PROVIDER_ERROR, "Feed transport failed", source=self.provider_id
                ) from exc
            if response.status_code == 429:
                raise BetterError(ErrorCode.RATE_LIMIT, "Feed rate limited", source=self.provider_id)
            if response.status_code >= 500:
                raise BetterError(
                    ErrorCode.TRANSIENT_PROVIDER_ERROR,
                    "Feed temporarily unavailable",
                    source=self.provider_id,
                )
            if response.status_code >= 400:
                raise BetterError(
                    ErrorCode.PERMANENT_PROVIDER_ERROR, "Feed request rejected", source=self.provider_id
                )
            try:
                raw = response.json()
                result = (
                    schema.validate_python(raw)
                    if isinstance(schema, TypeAdapter)
                    else schema.model_validate(raw)
                )
            except (ValueError, ValidationError) as exc:
                raise BetterError(
                    ErrorCode.SCHEMA_ERROR,
                    "Feed response failed contract validation",
                    source=self.provider_id,
                ) from exc
            return result

        result = await self.executor.call(request)
        if as_of:
            raw = (
                [m.model_dump(mode="json") for m in result]
                if isinstance(result, tuple)
                else result.model_dump(mode="json")
            )
            # This caches an as-of query result, not an assertion that the underlying observations are fresh.
            fetched_at = datetime.now(timezone.utc)
            self.cache.put(
                key,
                CacheEntry(
                    raw, fetched_at, fetched_at + timedelta(seconds=self.ttl_seconds), self.provider_id
                ),
            )
        return result

    async def list_matches(self, as_of: datetime) -> tuple[Match, ...]:
        return await self._fetch("/matches", TypeAdapter(tuple[Match, ...]), as_of)

    async def get_match(self, match_id: str) -> Match:
        return await self._fetch(f"/matches/{quote(match_id, safe='')}", Match)

    async def get_stats(self, match_id, as_of):
        return await self._fetch(f"/matches/{quote(match_id, safe='')}/stats", StatsSnapshot, as_of)

    async def get_odds(self, match_id, as_of):
        return await self._fetch(f"/matches/{quote(match_id, safe='')}/odds", OddsSnapshot, as_of)

    async def get_news(self, match_id, as_of):
        return await self._fetch(f"/matches/{quote(match_id, safe='')}/news", NewsSnapshot, as_of)

    async def get_lineups(self, match_id, as_of):
        return await self._fetch(f"/matches/{quote(match_id, safe='')}/lineups", LineupSnapshot, as_of)
