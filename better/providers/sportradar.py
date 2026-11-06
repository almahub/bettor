"""Sportradar Soccer v4 adapter with fail-closed identity and coverage checks."""

import asyncio
import copy
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import Field, SecretStr

from better.domain.common import Contract
from better.domain.enums import ErrorCode, ReliabilityTier, SourceType
from better.domain.errors import BetterError
from better.domain.models import LineupSnapshot, StatsSnapshot, TeamStats
from better.domain.provenance import Provenance
from better.providers.resilience import ProviderExecutor
from better.services.identity_resolution import SportradarIdentityResolver

SOURCE = "sportradar_soccer"
BASE_URL = "https://api.sportradar.com/soccer"


def read_api_key(path: Path = Path(".env")) -> SecretStr:
    if "SPORTRADAR_API_KEY" in os.environ:
        return SecretStr(os.environ["SPORTRADAR_API_KEY"].strip())
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "SPORTRADAR_API_KEY":
                return SecretStr(value.strip().strip('"').strip("'"))
    return SecretStr("")


class SportradarSettings(Contract):
    access_level: str = Field(default="trial", pattern="^(trial|production)$")
    language: str = Field(default="en", pattern="^[a-z]{2,3}$")
    sample_matches: int = Field(default=10, ge=2, le=30)
    monthly_call_budget: int = Field(default=900, ge=1, le=1000000)
    timeout_seconds: float = Field(default=12, gt=0, le=30)
    identity_minimum_name_score: float = Field(default=0.84, ge=0.7, le=1)
    identity_automatic_score: float = Field(default=0.92, ge=0.8, le=1)
    identity_minimum_margin: float = Field(default=0.08, ge=0.01, le=0.5)
    identity_maximum_kickoff_delta_minutes: int = Field(default=120, ge=15, le=360)
    identity_verified_kickoff_delta_minutes: int = Field(default=20, ge=1, le=60)
    competitions: dict[str, str] = Field(default_factory=dict)
    teams: dict[str, str] = Field(default_factory=dict)


class SportradarSoccerProvider:
    manages_execution = True

    def __init__(self, settings=None, *, fixtures=None, key=None, client=None, clock=None, repository=None):
        self.settings = settings or SportradarSettings()
        self.fixtures = fixtures
        self._key = key if key is not None else read_api_key()
        self.client = client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        self._owned = client is None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.repository = repository
        self.identity_resolver = SportradarIdentityResolver(
            minimum_name_score=self.settings.identity_minimum_name_score,
            automatic_score=self.settings.identity_automatic_score,
            minimum_margin=self.settings.identity_minimum_margin,
            maximum_kickoff_delta_minutes=self.settings.identity_maximum_kickoff_delta_minutes,
            verified_kickoff_delta_minutes=self.settings.identity_verified_kickoff_delta_minutes,
        )
        # Trial contract is 1 QPS. Keep a safety margin for edge timing at the gateway.
        self.executor = ProviderExecutor(self.settings.timeout_seconds, retries=0, rate_per_second=0.5)
        self._lock, self._cache = asyncio.Lock(), {}
        self._calls = 0
        self.last_error = None

    async def close(self):
        if self._owned:
            await self.client.aclose()

    def describe(self):
        result = {
            "provider": SOURCE,
            "configured": bool(self._key.get_secret_value()),
            "access_level": self.settings.access_level,
            "calls_this_process": self._calls,
            "local_budget": self.settings.monthly_call_budget,
            "mapped_competitions": len(self.settings.competitions),
            "mapped_teams": len(self.settings.teams),
            "last_error": self.last_error,
            "note": "La disponibilita effettiva viene verificata per ogni partita tramite i flag coverage.",
        }
        if self.repository is not None:
            result["identity_coverage"] = self.repository.identity_coverage()
        return result

    def _fail(self, code, message):
        self.last_error = message
        return BetterError(code, message, source=SOURCE)

    async def request(self, path, *, params=None, ttl=300):
        if not re.fullmatch(
            r"(?:competitions|schedules/\d{4}-\d{2}-\d{2}/summaries|competitors/sr:competitor:\d+/summaries|sport_events/sr:sport_event:\d+/lineups)\.json",
            path,
        ):
            raise ValueError("Unsupported Sportradar endpoint")
        if not self._key.get_secret_value():
            raise self._fail(ErrorCode.CONFIGURATION_ERROR, "SPORTRADAR_API_KEY assente nel file .env")
        params = params or {}
        cache_key = json.dumps([path, params], sort_keys=True)
        async with self._lock:
            now = self.clock()
            cached = self._cache.get(cache_key)
            if cached and now < cached[0]:
                return copy.deepcopy(cached[1])
            if self._calls >= self.settings.monthly_call_budget:
                raise self._fail(ErrorCode.RATE_LIMIT, "Budget locale Sportradar esaurito")

            async def fetch():
                self._calls += 1
                try:
                    return await self.client.get(
                        f"{BASE_URL}/{self.settings.access_level}/v4/{self.settings.language}/{path}",
                        params=params,
                        headers={"x-api-key": self._key.get_secret_value(), "accept": "application/json"},
                    )
                except httpx.TimeoutException:
                    raise self._fail(ErrorCode.TIMEOUT, "Sportradar: tempo di risposta superato") from None
                except httpx.TransportError:
                    raise self._fail(
                        ErrorCode.TRANSIENT_PROVIDER_ERROR, "Sportradar non raggiungibile"
                    ) from None

            response = await self.executor.call(fetch)
            if response.status_code != 200:
                code = (
                    ErrorCode.RATE_LIMIT
                    if response.status_code == 429
                    else ErrorCode.PERMANENT_PROVIDER_ERROR
                )
                raise self._fail(code, f"Sportradar: richiesta respinta (HTTP {response.status_code})")
            try:
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError()
            except (TypeError, ValueError):
                raise self._fail(ErrorCode.SCHEMA_ERROR, "Risposta Sportradar non valida") from None
            result = {
                "data": body,
                "retrieved_at": now,
                "url": str(response.url).replace(self._key.get_secret_value(), "[redacted]"),
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (now + timedelta(seconds=ttl), copy.deepcopy(result))
            return result

    async def check_access(self):
        response = await self.request("competitions.json", ttl=86400)
        rows = response["data"].get("competitions")
        if not isinstance(rows, list):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Catalogo competizioni Sportradar non valido")
        return {**self.describe(), "coverage": {"accessible": True, "competition_count": len(rows)}}

    def _provenance(self, response, field):
        return Provenance(
            source_id=SOURCE,
            source_type=SourceType.API,
            reliability_tier=ReliabilityTier.A,
            retrieved_at=response["retrieved_at"],
            field=field,
            url=response["url"],
            raw_reference="sha256:" + response["sha256"],
            primary_source_id="sportradar",
            notes="Coverage verificata sul singolo sport event; tier A indica fonte professionale, non completezza.",
        )

    async def _resolve(self, match_id):
        if self.fixtures is None:
            raise self._fail(ErrorCode.CONFIGURATION_ERROR, "Sorgente partite non collegata")
        match = await self.fixtures.get_match(match_id)
        response = await self.request(
            f"schedules/{match.kickoff.date().isoformat()}/summaries.json", params={"limit": 200}, ttl=300
        )
        rows = response["data"].get("summaries")
        if not isinstance(rows, list):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Programma giornaliero Sportradar non valido")
        persisted = {}
        if self.repository is not None:
            try:
                persisted = {
                    "match": self.repository.identity_target("match", match.match_id),
                    "home": self.repository.identity_target("team", match.home_team_id),
                    "away": self.repository.identity_target("team", match.away_team_id),
                    "competition": self.repository.identity_target("competition", match.competition),
                }
            except ValueError as error:
                raise self._fail(ErrorCode.SCHEMA_ERROR, str(error)) from None
        mapped = {
            "match": persisted.get("match"),
            "home": persisted.get("home") or self.settings.teams.get(match.home_team_id),
            "away": persisted.get("away") or self.settings.teams.get(match.away_team_id),
            "competition": persisted.get("competition") or self.settings.competitions.get(match.competition),
        }
        from better.providers.sport_888 import Sport888Provider

        fixture_record = (
            self.fixtures.latest_record(match_id) if isinstance(self.fixtures, Sport888Provider) else None
        )
        competition_name = fixture_record.competition_name if fixture_record else None
        candidates = self.identity_resolver.rank(
            match, rows, mapped=mapped, competition_name=competition_name
        )
        has_verified = all(mapped.values())
        selected, reason = self.identity_resolver.decide(candidates, has_verified_mapping=has_verified)
        diagnostics = [candidate.diagnostic() for candidate in candidates[:5]]
        now = self.clock()
        if self.repository is not None:
            self.repository.record_identity_attempt(
                match_id=match_id,
                attempted_at=now,
                result="RESOLVED" if selected else "FAILED",
                reason=reason,
                candidates=diagnostics,
                selected=selected.event_id if selected else None,
            )
        if selected is None:
            raise self._fail(
                ErrorCode.MISSING_DATA,
                f"Nessuna corrispondenza Sportradar univoca: {reason} ({len(candidates)} candidati)",
            )
        if self.repository is not None and reason == "AUTO_VERIFIED":
            evidence = {
                "sportradar_schedule_sha256": response["sha256"],
                "candidate": selected.diagnostic(),
                "fixture_provenance": fixture_record.raw_hash if fixture_record else None,
            }
            for kind, canonical, target in (
                ("match", match.match_id, selected.event_id),
                ("team", match.home_team_id, selected.home_id),
                ("team", match.away_team_id, selected.away_id),
                ("competition", match.competition, selected.competition_id),
            ):
                self.repository.save_identity_mapping(
                    kind=kind,
                    canonical_id=canonical,
                    target_id=target,
                    confidence=selected.score,
                    method="AUTO_HIGH_CONFIDENCE",
                    verified_at=now,
                    evidence=evidence,
                )
        return match, selected.row, selected.competitors

    async def get_stats(self, match_id, as_of):
        if abs((self.clock() - as_of).total_seconds()) > 300:
            raise self._fail(ErrorCode.MISSING_DATA, "Sportradar non ricostruisce snapshot storici as-of")
        match, _, competitors = await self._resolve(match_id)
        profiles, sources, cutoffs = [], [], []
        for side, canonical in (("home", match.home_team_id), ("away", match.away_team_id)):
            native_id = competitors[side]["id"]
            response = await self.request(f"competitors/{native_id}/summaries.json", ttl=3600)
            summaries = response["data"].get("summaries")
            if not isinstance(summaries, list):
                raise self._fail(ErrorCode.SCHEMA_ERROR, "Storico Sportradar non valido")
            rows = []
            for item in summaries:
                try:
                    event, status = item["sport_event"], item["sport_event_status"]
                    date = datetime.fromisoformat(event["start_time"])
                    if date + timedelta(hours=3) >= min(as_of, match.kickoff) or status["status"] != "closed":
                        continue
                    teams = {team["qualifier"]: team["id"] for team in event["competitors"]}
                    own_side = (
                        "home"
                        if teams["home"] == native_id
                        else "away"
                        if teams["away"] == native_id
                        else None
                    )
                    if own_side is None:
                        raise ValueError()
                    other_side = "away" if own_side == "home" else "home"
                    gf = status.get(f"{own_side}_normaltime_score")
                    ga = status.get(f"{other_side}_normaltime_score")
                    if gf is None or ga is None:
                        periods = [
                            period
                            for period in status.get("period_scores", [])
                            if period.get("type") == "regular_period" and period.get("number") in {1, 2}
                        ]
                        if {period.get("number") for period in periods} != {1, 2}:
                            raise ValueError()
                        gf = sum(period[f"{own_side}_score"] for period in periods)
                        ga = sum(period[f"{other_side}_score"] for period in periods)
                    if type(gf) is not int or type(ga) is not int:
                        raise ValueError()
                    rows.append((date, event["id"], gf, ga, own_side))
                except (KeyError, TypeError, ValueError):
                    # A competitor feed can include abandoned or structurally older games.
                    # They are excluded rather than converted into regulation-time scores.
                    continue
            rows.sort(reverse=True)
            rows = rows[: self.settings.sample_matches]
            if len(rows) < 2:
                raise self._fail(ErrorCode.MISSING_DATA, "Campione Sportradar insufficiente")
            if len({row[1] for row in rows}) != len(rows):
                raise self._fail(ErrorCode.SCHEMA_ERROR, "Storico Sportradar contiene partite duplicate")

            def rates(values):
                return (
                    {
                        "goals_for": sum(x[2] for x in values) / len(values),
                        "goals_against": sum(x[3] for x in values) / len(values),
                    }
                    if values
                    else {}
                )

            segments = {"last_5": rates(rows[:5]), "last_10": rates(rows[:10]), "season": rates(rows)}
            home_rows, away_rows = [x for x in rows if x[4] == "home"], [x for x in rows if x[4] == "away"]
            if home_rows:
                segments["home"] = rates(home_rows)
            if away_rows:
                segments["away"] = rates(away_rows)
            profiles.append(
                TeamStats(
                    team_id=canonical,
                    sample_size=len(rows),
                    goals_for_per_match=sum(x[2] for x in rows) / len(rows),
                    goals_against_per_match=sum(x[3] for x in rows) / len(rows),
                    segments=segments,
                )
            )
            sources.append(self._provenance(response, f"{side}.results"))
            cutoffs.append(max(x[0] + timedelta(hours=3) for x in rows))
        return StatsSnapshot(
            snapshot_id="sportradar:"
            + hashlib.sha256("".join(x.raw_reference for x in sources).encode()).hexdigest(),
            match_id=match_id,
            home=profiles[0],
            away=profiles[1],
            data_cutoff=max(cutoffs),
            captured_at=max(x.retrieved_at for x in sources),
            provenance=tuple(sources),
        )

    async def get_lineups(self, match_id, as_of):
        if abs((self.clock() - as_of).total_seconds()) > 300:
            raise self._fail(ErrorCode.MISSING_DATA, "Sportradar non ricostruisce formazioni storiche as-of")
        _, row, competitors = await self._resolve(match_id)
        properties = row["sport_event"].get("coverage", {}).get("sport_event_properties", {})
        if not properties.get("lineups") or properties.get("lineups_availability") != "pre":
            raise self._fail(ErrorCode.MISSING_DATA, "Formazioni pre-partita non coperte da Sportradar")
        event_id = row["sport_event"]["id"]
        response = await self.request(f"sport_events/{event_id}/lineups.json", ttl=60)
        if isinstance(response["data"].get("message"), str):
            raise self._fail(ErrorCode.MISSING_DATA, "Formazioni Sportradar non ancora disponibili")
        lineups_node = response["data"].get("lineups")
        lineups = lineups_node.get("competitors") if isinstance(lineups_node, dict) else lineups_node
        if not isinstance(lineups, list):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Formazioni Sportradar non valide")
        try:
            by_team = {
                (item.get("competitor") or item)["id"]: tuple(
                    p["name"] for p in item["players"] if p.get("starter") is True
                )
                for item in lineups
            }
            home, away = (
                by_team.get(competitors["home"]["id"], ()),
                by_team.get(competitors["away"]["id"], ()),
            )
        except (KeyError, TypeError):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Giocatori Sportradar non validi") from None
        if len(home) != 11 or len(away) != 11 or len(set(home)) != 11 or len(set(away)) != 11:
            raise self._fail(ErrorCode.MISSING_DATA, "Formazioni Sportradar complete non disponibili")
        confirmed = bool(
            response["data"]
            .get("sport_event", {})
            .get("sport_event_conditions", {})
            .get("lineups", {})
            .get("confirmed")
        )
        # Sportradar is an API provider; do not upgrade it to official Tier A confirmation.
        return LineupSnapshot(
            snapshot_id="sportradar:" + response["sha256"],
            match_id=match_id,
            status="PROBABLE_LINEUP",
            home_players=home,
            away_players=away,
            captured_at=response["retrieved_at"],
            provenance=(
                self._provenance(response, "lineups.confirmed_upstream" if confirmed else "lineups"),
            ),
        )
