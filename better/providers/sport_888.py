"""Read-only 888 Italy public catalogue/odds adapter, observed 2026-09-28.

This is the website's public Kambi offering interface, not a contracted stable
API. No account endpoints, sessions, bet placement, or synthetic fallbacks.
"""

import asyncio
import copy
import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from pydantic import Field

from better.domain.common import Contract
from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.domain.models import Market, Match, OddsQuote, OddsSnapshot
from better.domain.provenance import Provenance
from better.providers.resilience import ProviderExecutor

BASE_URL = "https://eu-offering-api.kambicdn.com/offering/v2018/888it"
CATALOG_PATH = "/event/group/1000093190"
PROVIDER = "888sport_it"


class Sport888Settings(Contract):
    catalog_hours: int = Field(default=72, ge=1, le=336)
    catalog_ttl_seconds: int = Field(default=120, ge=1, le=600)
    odds_ttl_seconds: int = Field(default=15, ge=1, le=60)
    timeout_seconds: float = Field(default=8, gt=0, le=30)
    allowed_competition_patterns: tuple[str, ...] = ()
    # Native competition ID -> expected display-name pattern. When configured,
    # both identity and label must match: 888 occasionally reuses/mislabels
    # generic league names, so a text-only allowlist is not a safe country filter.
    allowed_competitions: dict[int, str] = Field(default_factory=dict)


class FixtureRecord(Contract):
    match: Match
    competition_name: str
    provenance: Provenance
    provider: str = PROVIDER
    raw_hash: str


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timezone")
    return parsed.astimezone(UTC)


def integer(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Expected positive integer")
    return value


class Sport888Provider:
    manages_execution = True

    def __init__(self, settings=None, *, client=None, clock=None):
        self.settings = settings or Sport888Settings()
        self.client = client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        self._owned_client = client is None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.executor = ProviderExecutor(timeout=self.settings.timeout_seconds, retries=1, rate_per_second=2)
        self._cache = {}
        self._records = {}
        self._lock = asyncio.Lock()
        self.catalog_metadata = {}

    @staticmethod
    def external_id(match_id):
        if not re.fullmatch(r"888it:match:[1-9][0-9]*", match_id):
            raise BetterError(
                ErrorCode.CONFIGURATION_ERROR, "Expected 888it:match:<event id>", source=PROVIDER
            )
        return int(match_id.rsplit(":", 1)[1])

    async def _request(self, path, ttl):
        # One lock coalesces catalogue requests from concurrent page/analysis loads.
        async with self._lock:
            now = self.clock()
            cached = self._cache.get(path)
            if cached and now < cached[0] + timedelta(seconds=ttl):
                return copy.deepcopy(cached[1])

            async def fetch():
                try:
                    response = await self.client.get(
                        BASE_URL + path,
                        params={
                            "lang": "it_IT",
                            "market": "IT",
                            "includeParticipants": "true",
                        },
                    )
                except httpx.HTTPError:
                    raise BetterError(
                        ErrorCode.TRANSIENT_PROVIDER_ERROR, "888 public feed unavailable", source=PROVIDER
                    ) from None
                if response.status_code != 200:
                    code = (
                        ErrorCode.RATE_LIMIT
                        if response.status_code == 429
                        else (
                            ErrorCode.TRANSIENT_PROVIDER_ERROR
                            if response.status_code >= 500
                            else ErrorCode.PERMANENT_PROVIDER_ERROR
                        )
                    )
                    raise BetterError(code, f"888 public feed HTTP {response.status_code}", source=PROVIDER)
                retrieved = self.clock()
                try:
                    data = response.json()
                    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                        raise ValueError("Missing events")
                    if any(not isinstance(e, dict) or type(e.get("id")) is not int for e in data["events"]):
                        raise ValueError("Malformed event identities")
                    dated = parsedate_to_datetime(response.headers["date"])
                    if dated.tzinfo is None:
                        raise ValueError("HTTP date must have a timezone")
                    dated = dated.astimezone(UTC)
                    age = int(response.headers.get("age", "0"))
                    if age < 0 or dated > retrieved + timedelta(seconds=5):
                        raise ValueError("Invalid source time")
                    observed = min(dated, retrieved - timedelta(seconds=age), retrieved)
                    raw_hash = hashlib.sha256(response.content).hexdigest()
                except (ValueError, KeyError, TypeError, OverflowError):
                    raise BetterError(
                        ErrorCode.SCHEMA_ERROR, "888 response/schema/date not recognized", source=PROVIDER
                    ) from None
                return data, retrieved, observed, raw_hash, str(response.url)

            record = await self.executor.call(fetch)
            self._cache[path] = (record[1], record)
            # Bounded cache; opening many selected matches does not grow without limit.
            if len(self._cache) > 100:
                self._cache.pop(next(iter(self._cache)))
            return copy.deepcopy(record)

    @staticmethod
    def _match(event, now):
        if (
            event.get("sport") != "FOOTBALL"
            or event.get("state") != "NOT_STARTED"
            or "MATCH" not in event.get("tags", [])
        ):
            return None
        start = instant(event["start"])
        if start <= now:
            return None
        identifier = integer(event["id"])
        participants = event.get("participants", [])
        home = [p for p in participants if p.get("home") is True and p.get("participantType") == "TEAM"]
        away = [p for p in participants if p.get("home") is False and p.get("participantType") == "TEAM"]
        if len(home) != 1 or len(away) != 1:
            raise ValueError("Unresolved native team identities")
        if home[0].get("name") != event.get("homeName") or away[0].get("name") != event.get("awayName"):
            raise ValueError("Participant side/name mismatch")
        if any(p.get("scratched") or p.get("nonRunner") for p in (home[0], away[0])):
            return None
        return Match(
            match_id=f"888it:match:{identifier}",
            competition=f"888it:competition:{integer(event['groupId'])}",
            season="unknown",
            kickoff=start,
            home_team=event["homeName"],
            away_team=event["awayName"],
            home_team_id=f"888it:team:{integer(home[0]['participantId'])}",
            away_team_id=f"888it:team:{integer(away[0]['participantId'])}",
        )

    @staticmethod
    def _provenance(retrieved, url, raw_hash, field, published=None):
        return Provenance(
            source_id=PROVIDER,
            source_type="MARKET",
            url=url,
            retrieved_at=retrieved,
            published_at=published,
            reliability_tier="B",
            field=field,
            raw_reference=f"sha256:{raw_hash}:{field}",
            primary_source_id="888it-kambi",
            notes="Public website offering feed; season and liquidity not supplied. Native IDs are not cross-provider aliases.",
        )

    def latest_record(self, match_id):
        record = self._records.get(match_id)
        return record.model_copy(deep=True) if record else None

    def competition_names(self):
        return {r.match.competition: r.competition_name for r in self._records.values()}

    async def list_matches(self, as_of):
        data, retrieved, observed, raw_hash, url = await self._request(
            CATALOG_PATH, self.settings.catalog_ttl_seconds
        )
        if retrieved > as_of + timedelta(seconds=self.settings.timeout_seconds + 5):
            raise BetterError(
                ErrorCode.MISSING_DATA,
                "888 is a current feed, not a historical fixture archive",
                source=PROVIDER,
            )
        if self.clock() - observed > timedelta(seconds=600):
            raise BetterError(ErrorCode.STALE_DATA, "888 catalogue response is stale", source=PROVIDER)
        matches, skipped, seen = [], 0, set()
        records = {}
        identifiers = [e.get("id") for e in data["events"] if isinstance(e, dict)]
        if len(identifiers) != len(set(identifiers)):
            raise BetterError(
                ErrorCode.SCHEMA_ERROR, "888 catalogue has duplicate event IDs", source=PROVIDER
            )
        for event in data["events"]:
            try:
                match = self._match(event, max(as_of, self.clock()))
                if match is None or match.kickoff > as_of + timedelta(hours=self.settings.catalog_hours):
                    continue
                if self.settings.allowed_competition_patterns and not any(
                    re.fullmatch(pattern, str(event.get("group", "")).strip(), re.IGNORECASE)
                    for pattern in self.settings.allowed_competition_patterns
                ):
                    continue
                if self.settings.allowed_competitions:
                    group_id = integer(event.get("groupId"))
                    expected_name = self.settings.allowed_competitions.get(group_id)
                    if expected_name is None or not re.fullmatch(
                        expected_name,
                        str(event.get("group", "")).strip(),
                        re.IGNORECASE,
                    ):
                        continue
                if match.match_id in seen:
                    raise ValueError("Duplicate native fixture")
                seen.add(match.match_id)
                matches.append(match)
                records[match.match_id] = FixtureRecord(
                    match=match,
                    competition_name=event["group"],
                    raw_hash=raw_hash,
                    provenance=self._provenance(retrieved, url, raw_hash, f"fixture:{event['id']}"),
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                skipped += 1
        self._records = records
        self.catalog_metadata = {
            "provider": PROVIDER,
            "retrieved_at": retrieved.isoformat(),
            "hours": self.settings.catalog_hours,
            "available": len(matches),
            "competition_filter": list(self.settings.allowed_competition_patterns),
            "competition_bindings": dict(self.settings.allowed_competitions),
            "unresolved_skipped": skipped,
            "raw_hash": raw_hash,
        }
        return tuple(sorted(matches, key=lambda m: (m.kickoff, m.match_id)))

    async def get_match(self, match_id):
        self.external_id(match_id)
        matches = await self.list_matches(self.clock())
        match = next((m for m in matches if m.match_id == match_id), None)
        if match is None:
            raise BetterError(
                ErrorCode.MISSING_DATA,
                "888 fixture is unavailable, unresolved or outside the prematch window",
                source=PROVIDER,
            )
        return match

    async def get_odds(self, match_id, as_of):
        identifier = self.external_id(match_id)
        data, retrieved, observed, raw_hash, url = await self._request(
            f"/betoffer/event/{identifier}", self.settings.odds_ttl_seconds
        )
        try:
            events = [e for e in data["events"] if e.get("id") == identifier]
            if len(events) != 1 or self._match(events[0], self.clock()) is None:
                raise ValueError("Event is no longer prematch")
            native_match = self._match(events[0], self.clock())
            prior = self.latest_record(match_id)
            if prior and native_match != prior.match:
                raise ValueError("Selected fixture changed before odds retrieval")
            quotes = []
            seen = set()
            for offer in data["betOffers"]:
                # Pin native criterion AND offer type; never confuse halves, team
                # totals, handicaps, extra time or bet-builder combinations.
                key = (offer.get("criterion", {}).get("id"), offer.get("betOfferType", {}).get("id"))
                mapping = {
                    (1001159858, 2): ("1X2", {"OT_ONE": "HOME", "OT_CROSS": "DRAW", "OT_TWO": "AWAY"}),
                    (1001159926, 6): ("OVER_UNDER_2_5", {"OT_OVER": "OVER_2_5", "OT_UNDER": "UNDER_2_5"}),
                    (1001642858, 18): ("BTTS", {"OT_YES": "YES", "OT_NO": "NO"}),
                }
                if (
                    key not in mapping
                    or offer.get("eventId") != identifier
                    or "OFFERED_PREMATCH" not in offer.get("tags", [])
                ):
                    continue
                market, selections = mapping[key]
                outcomes = offer.get("outcomes", [])
                if market == "OVER_UNDER_2_5" and any(o.get("line") != 2500 for o in outcomes):
                    continue
                if len(outcomes) != len(selections) or {o.get("type") for o in outcomes} != set(selections):
                    continue
                if any(o.get("status") != "OPEN" for o in outcomes):
                    continue
                if market in seen:
                    raise ValueError("Ambiguous duplicate market")
                seen.add(market)
                for outcome in outcomes:
                    if outcome.get("betOfferId", offer["id"]) != offer["id"]:
                        raise ValueError("Foreign offer outcome")
                    if market == "1X2" and outcome.get("participantId") is not None:
                        team_id = (
                            native_match.home_team_id
                            if outcome["type"] == "OT_ONE"
                            else (native_match.away_team_id if outcome["type"] == "OT_TWO" else None)
                        )
                        if team_id != f"888it:team:{integer(outcome['participantId'])}":
                            raise ValueError("Outcome participant mismatch")
                    changed = instant(outcome["changedDate"]) if outcome.get("changedDate") else None
                    quotes.append(
                        OddsQuote(
                            match_id=match_id,
                            bookmaker="888sport.it",
                            market=Market(
                                type=market,
                                selection=selections[outcome["type"]],
                                line=2.5 if market == "OVER_UNDER_2_5" else None,
                            ),
                            odds=integer(outcome["odds"]) / 1000,
                            captured_at=observed,
                            liquidity=None,
                            provenance=self._provenance(
                                retrieved,
                                url,
                                raw_hash,
                                f"event:{identifier}:offer:{offer['id']}:outcome:{outcome['id']}",
                                changed,
                            ),
                        )
                    )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise BetterError(
                ErrorCode.SCHEMA_ERROR, "888 event/odds mapping is incomplete or ambiguous", source=PROVIDER
            ) from None
        if not quotes:
            raise BetterError(
                ErrorCode.MISSING_DATA, "888 has no complete open supported prematch market", source=PROVIDER
            )
        return OddsSnapshot(
            snapshot_id=f"888it:{identifier}:{raw_hash}", match_id=match_id, quotes=tuple(quotes)
        )

    async def close(self):
        if self._owned_client:
            await self.client.aclose()
