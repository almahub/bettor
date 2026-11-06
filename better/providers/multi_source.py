"""Primary/secondary provider comparison with explicit, fail-closed material conflicts."""

import asyncio
import json

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.domain.models import LineupSnapshot, Match, NewsSnapshot, OddsSnapshot, StatsSnapshot
from better.providers.resilience import ProviderExecutor


class MultiSourceProvider:
    """Wrap matching typed provider interfaces without silently resolving disagreements.

    Fallback never handles schema errors or conflicts. Tolerated differences preserve the
    primary observation; comparison_records retains both full payloads for audit sinks.
    """

    unavailable_codes = frozenset(
        {
            ErrorCode.TRANSIENT_PROVIDER_ERROR,
            ErrorCode.PERMANENT_PROVIDER_ERROR,
            ErrorCode.RATE_LIMIT,
            ErrorCode.TIMEOUT,
            ErrorCode.MISSING_DATA,
        }
    )

    def __init__(
        self,
        primary,
        secondary,
        *,
        stats_tolerance=0.25,
        odds_relative_tolerance=0.03,
        odds_timestamp_tolerance_seconds=60,
    ):
        if (
            stats_tolerance < 0
            or not 0 <= odds_relative_tolerance < 1
            or odds_timestamp_tolerance_seconds < 0
        ):
            raise ValueError("Source comparison tolerances must be nonnegative and odds tolerance below one")
        self.primary, self.secondary = primary, secondary
        self.stats_tolerance = stats_tolerance
        self.odds_relative_tolerance = odds_relative_tolerance
        self.odds_timestamp_tolerance_seconds = odds_timestamp_tolerance_seconds
        self.comparison_records = []
        self.manages_execution = True
        self._executors = {id(source): ProviderExecutor() for source in (primary, secondary)}

    async def _invoke(self, source, method, args):
        def call():
            return getattr(source, method)(*args)

        if hasattr(source, "executor") or getattr(source, "manages_execution", False):
            return await call()
        return await self._executors[id(source)].call(call)

    async def _call(self, method, *args):
        outcomes = await asyncio.gather(
            self._invoke(self.primary, method, args),
            self._invoke(self.secondary, method, args),
            return_exceptions=True,
        )
        primary, secondary = outcomes

        def raw(result):
            if isinstance(result, BetterError):
                return {"error": result.detail.model_dump(mode="json")}
            if isinstance(result, BaseException):
                return {"error": type(result).__name__}
            if isinstance(result, tuple):
                return [value.model_dump(mode="json") for value in result]
            return result.model_dump(mode="json")

        record = {
            "operation": method,
            "primary": raw(primary),
            "secondary": raw(secondary),
            "action": "COMPARE",
        }
        self.comparison_records.append(record)
        if isinstance(primary, BaseException):
            if isinstance(primary, asyncio.CancelledError):
                raise primary
            if not isinstance(primary, BetterError) or primary.detail.code not in self.unavailable_codes:
                record["action"] = "PRIMARY_INVALID"
                raise primary
            if isinstance(secondary, BaseException):
                record["action"] = "BOTH_UNAVAILABLE"
                raise primary
            record["action"] = "SECONDARY_FALLBACK"
            return secondary
        if isinstance(secondary, BaseException):
            if isinstance(secondary, asyncio.CancelledError):
                raise secondary
            if isinstance(secondary, BetterError) and secondary.detail.code not in self.unavailable_codes:
                record["action"] = "SECONDARY_INVALID"
                raise secondary
            if not isinstance(secondary, BetterError):
                raise secondary
            record["action"] = "PRIMARY_ONLY_SECONDARY_UNAVAILABLE"
            return primary
        conflicts = self._conflicts(primary, secondary)
        if conflicts:
            record["action"] = "SOURCE_CONFLICT"
            record["conflicts"] = conflicts
            # Preserve specific fields/values in the error envelope persisted by the orchestrator.
            raise BetterError(
                ErrorCode.SCHEMA_ERROR,
                "SOURCE_CONFLICT: " + json.dumps(conflicts, sort_keys=True),
                source="multi_source",
            )
        record["action"] = "PRIMARY_WITH_COMPARISON"
        if isinstance(primary, NewsSnapshot):
            return primary.model_copy(
                update={
                    "items": primary.items + secondary.items,
                    "provenance": primary.provenance + secondary.provenance,
                    "captured_at": min(primary.captured_at, secondary.captured_at),
                }
            )
        return primary

    def _conflicts(self, primary, secondary):
        conflicts = []

        def compare(field, one, two, tolerance=0):
            differs = (
                abs(one - two) > tolerance
                if isinstance(one, (int, float)) and isinstance(two, (int, float))
                else one != two
            )
            if differs:
                conflicts.append(
                    {"field": field, "primary": str(one), "secondary": str(two), "tolerance": tolerance}
                )

        if type(primary) is not type(secondary):
            return [
                {
                    "field": "schema_type",
                    "primary": type(primary).__name__,
                    "secondary": type(secondary).__name__,
                }
            ]
        if isinstance(primary, tuple):
            second = {match.match_id: match for match in secondary}
            for match in primary:
                if match.match_id in second:
                    conflicts.extend(self._conflicts(match, second[match.match_id]))
            return conflicts
        compare("match_id", primary.match_id, secondary.match_id)
        if isinstance(primary, Match):
            for key in ("home_team_id", "away_team_id", "kickoff", "competition"):
                compare(key, getattr(primary, key), getattr(secondary, key))
        elif isinstance(primary, StatsSnapshot):
            for side in ("home", "away"):
                one, two = getattr(primary, side), getattr(secondary, side)
                compare(f"{side}.team_id", one.team_id, two.team_id)
                compare(f"{side}.sample_size", one.sample_size, two.sample_size)
                for metric in (
                    "goals_for_per_match",
                    "goals_against_per_match",
                    "xg_per_match",
                    "xga_per_match",
                ):
                    left, right = getattr(one, metric), getattr(two, metric)
                    if left is not None and right is not None:
                        compare(f"{side}.{metric}", left, right, self.stats_tolerance)
                for segment in set(one.segments) & set(two.segments):
                    for metric in set(one.segments[segment]) & set(two.segments[segment]):
                        compare(
                            f"{side}.{segment}.{metric}",
                            one.segments[segment][metric],
                            two.segments[segment][metric],
                            self.stats_tolerance,
                        )
        elif isinstance(primary, OddsSnapshot):

            def complete_markets(snapshot):
                grouped = {}
                for quote in snapshot.quotes:
                    key = (quote.bookmaker, quote.market.type.value, quote.market.line, quote.captured_at)
                    grouped.setdefault(key, {})[quote.market.selection] = quote
                latest = {}
                selections = {
                    "1X2": {"HOME", "DRAW", "AWAY"},
                    "OVER_UNDER_2_5": {"OVER_2_5", "UNDER_2_5"},
                    "BTTS": {"YES", "NO"},
                }
                for (book, market, line, captured), quotes in grouped.items():
                    if set(quotes) != selections.get(market, set()):
                        continue
                    key = (book, market, line)
                    if key not in latest or captured > latest[key][0]:
                        latest[key] = (captured, quotes)
                return latest

            one, two = complete_markets(primary), complete_markets(secondary)
            for key in set(one) & set(two):
                first_time, first = one[key]
                second_time, second = two[key]
                if abs((first_time - second_time).total_seconds()) > self.odds_timestamp_tolerance_seconds:
                    continue
                for selection in first:
                    left, right = first[selection].odds, second[selection].odds
                    tolerance = min(left, right) * self.odds_relative_tolerance
                    compare(f"odds.{key}.{selection}", left, right, tolerance)
        elif isinstance(primary, LineupSnapshot):
            if primary.status == secondary.status == "CONFIRMED_LINEUP":
                compare("home_players", set(primary.home_players), set(secondary.home_players))
                compare("away_players", set(primary.away_players), set(secondary.away_players))
        return conflicts

    async def list_matches(self, as_of):
        return await self._call("list_matches", as_of)

    async def get_match(self, match_id):
        return await self._call("get_match", match_id)

    async def get_stats(self, match_id, as_of):
        return await self._call("get_stats", match_id, as_of)

    async def get_odds(self, match_id, as_of):
        return await self._call("get_odds", match_id, as_of)

    async def get_news(self, match_id, as_of):
        return await self._call("get_news", match_id, as_of)

    async def get_lineups(self, match_id, as_of):
        return await self._call("get_lineups", match_id, as_of)

    def latest_record(self, match_id):
        relevant = [record for record in self.comparison_records if record["operation"] == "get_match"]
        source = (
            self.secondary if relevant and relevant[-1]["action"] == "SECONDARY_FALLBACK" else self.primary
        )
        return source.latest_record(match_id) if hasattr(source, "latest_record") else None

    async def close(self):
        closed = set()
        for source in (self.primary, self.secondary):
            if id(source) not in closed and hasattr(source, "close"):
                await source.close()
                closed.add(id(source))
