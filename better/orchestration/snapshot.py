import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from pydantic import ValidationError

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.domain.models import LineupSnapshot, NewsSnapshot, OddsSnapshot, StatsSnapshot
from better.providers.base import ProviderBundle
from better.providers.resilience import ProviderExecutor

logger = logging.getLogger("better.sources")


@dataclass
class SnapshotProviders:
    match: object
    snapshots: dict
    failures: dict

    def _get(self, name, match_id):
        if match_id != self.match.match_id:
            raise BetterError(ErrorCode.MISSING_DATA, "Snapshot belongs to a different match")
        if name in self.failures:
            raise self.failures[name]
        # Defensive deep copies stop one agent mutating data seen by another agent.
        return self.snapshots[name].model_copy(deep=True)

    async def get_match(self, match_id):
        if match_id != self.match.match_id:
            raise BetterError(ErrorCode.MISSING_DATA, "Unknown match")
        return self.match.model_copy(deep=True)

    async def list_matches(self, as_of):
        return (self.match.model_copy(deep=True),)

    async def get_stats(self, match_id, as_of):
        return self._get("stats", match_id)

    async def get_odds(self, match_id, as_of):
        return self._get("odds", match_id)

    async def get_news(self, match_id, as_of):
        return self._get("news", match_id)

    async def get_lineups(self, match_id, as_of):
        return self._get("lineups", match_id)


class SnapshotCollector:
    def __init__(self, providers, settings):
        self.providers = providers
        self.executors = {
            name: ProviderExecutor(
                timeout=settings.provider_timeout_seconds, retries=settings.provider_retries
            )
            for name in ("stats", "odds", "news", "lineups", "fixtures")
        }

    async def collect(self, match, as_of, *, live=False):
        snapshots, failures = {}, {}
        expected = {
            "stats": StatsSnapshot,
            "odds": OddsSnapshot,
            "news": NewsSnapshot,
            "lineups": LineupSnapshot,
        }

        async def fetch(name):
            started = perf_counter()
            provider = getattr(self.providers, name)
            method = getattr(provider, f"get_{name}")
            try:
                # Concrete HTTP adapters already enforce their rate/timeout/retry budget.
                result = (
                    await method(match.match_id, as_of)
                    if hasattr(provider, "executor") or getattr(provider, "manages_execution", False)
                    else await self.executors[name].call(lambda: method(match.match_id, as_of))
                )
                result = expected[name].model_validate(result.model_dump())
                cutoff = datetime.now(timezone.utc) if live else as_of
                if result.match_id != match.match_id:
                    raise BetterError(ErrorCode.SCHEMA_ERROR, "Snapshot match mismatch", source=name)
                if name == "odds":
                    result = result.model_copy(
                        update={
                            "quotes": tuple(
                                q
                                for q in result.quotes
                                if q.captured_at <= cutoff and q.provenance.available_at(cutoff)
                            )
                        }
                    )
                else:
                    if result.captured_at > cutoff or any(
                        not p.available_at(cutoff) for p in result.provenance
                    ):
                        raise BetterError(
                            ErrorCode.MISSING_DATA, "Snapshot unavailable at as_of", source=name
                        )
                    if name == "stats" and result.data_cutoff > cutoff:
                        raise BetterError(
                            ErrorCode.MISSING_DATA, "Statistics cutoff after as_of", source=name
                        )
                    if name == "news":
                        result = result.model_copy(
                            update={
                                "items": tuple(
                                    item for item in result.items if item.provenance.available_at(cutoff)
                                )
                            }
                        )
                snapshots[name] = result
            except BetterError as error:
                failures[name] = error
            except (ValidationError, AttributeError, TypeError, ValueError):
                failures[name] = BetterError(
                    ErrorCode.SCHEMA_ERROR, "Malformed provider snapshot", source=name
                )
            except Exception:
                failures[name] = BetterError(
                    ErrorCode.PERMANENT_PROVIDER_ERROR, "Unexpected provider failure", source=name
                )
            finally:
                logger.info(
                    "source_collection_completed",
                    extra={
                        "match_id": match.match_id,
                        "source_kind": name,
                        "provider": type(provider).__name__,
                        "status": failures[name].detail.code.value if name in failures else "OK",
                        "duration_ms": (perf_counter() - started) * 1000,
                    },
                )

        await asyncio.gather(*(fetch(name) for name in expected))
        return self.freeze(match, snapshots, failures), snapshots, failures

    def freeze(self, match, snapshots, failures):
        frozen = SnapshotProviders(match, snapshots, failures)
        return ProviderBundle(
            frozen,
            frozen,
            frozen,
            frozen,
            frozen,
            self.providers.validation_history,
            self.providers.portfolio,
            self.providers.system_two,
        )
