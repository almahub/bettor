"""API-Sports v3 adapter. No synthetic fallback or historical timestamp backdating."""

import asyncio
import copy
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import Field, PositiveInt, SecretStr

from better.domain.common import Contract
from better.domain.enums import ErrorCode, ReliabilityTier, SourceType
from better.domain.errors import BetterError
from better.domain.models import LineupSnapshot, RecentTeamMatch, StatsSnapshot, TeamStats
from better.domain.provenance import Provenance
from better.providers.resilience import ProviderExecutor
from better.providers.result_identity import NORMALIZATION_VERSION, normalize_team_name
from better.services.identity_resolution import name_similarity, normalized_name

BASE_URL = "https://v3.football.api-sports.io"
SOURCE = "api_football"


def read_api_key(path: Path = Path(".env")) -> SecretStr:
    """Read only this credential; never execute/interpolate dotenv contents."""
    if "API_FOOTBALL_API_KEY" in os.environ:
        return SecretStr(os.environ["API_FOOTBALL_API_KEY"].strip())
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "API_FOOTBALL_API_KEY":
                return SecretStr(value.strip().strip('"').strip("'"))
    return SecretStr("")


class CompetitionBinding(Contract):
    league: int = Field(gt=0)
    season: int = Field(ge=2000, le=2100)


class APIFootballSettings(Contract):
    network_enabled: bool = True
    results_enabled: bool = True
    competitions: dict[str, CompetitionBinding] = Field(default_factory=dict)
    # Explicit bookmaker team identity -> native API team identity, never fuzzy matching.
    teams: dict[str, PositiveInt] = Field(default_factory=dict)
    sample_matches: int = Field(default=10, ge=2, le=30)
    timeout_seconds: float = Field(default=12, gt=0, le=30)
    daily_request_budget: int = Field(default=90, ge=1, le=7500)
    archive_enabled: bool = False
    analysis_archive_only: bool = False
    archive_seasons: tuple[int, ...] = Field(default=(2026, 2025, 2024), min_length=1)
    archive_leagues: tuple[PositiveInt, ...] = Field(default=(135,), min_length=1)
    archive_batch_requests: int = Field(default=4, ge=1, le=10)
    archive_daily_requests: int = Field(default=100, ge=1, le=1000)
    identity_minimum_name_score: float = Field(default=0.84, ge=0, le=1)
    identity_automatic_score: float = Field(default=0.92, ge=0, le=1)
    identity_minimum_margin: float = Field(default=0.08, ge=0, le=1)


class APIFootballProvider:
    manages_execution = True

    def __init__(self, settings=None, *, fixtures=None, repository=None, key=None, client=None, clock=None):
        self.settings = settings or APIFootballSettings()
        self.fixtures = fixtures
        self.repository = repository
        self._key = key if key is not None else read_api_key()
        self.client = client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        self._owned = client is None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.executor = ProviderExecutor(self.settings.timeout_seconds, retries=0, rate_per_second=1 / 6.1)
        self._lock = asyncio.Lock()
        self.archive_lock = asyncio.Lock()
        self._cache = {}
        self._blocked_seasons = {}
        self._daily_calls = 0
        self._day = self.clock().date()
        self._account = None
        self._coverage = None
        self._request_log = []
        self.last_error = None
        from better.services.football_archive import FootballArchive

        self.archive = FootballArchive(repository, self.settings.archive_seasons, self.settings.archive_leagues) if repository else None

    async def close(self):
        if self._owned:
            await self.client.aclose()

    def describe(self):
        return {
            "provider": SOURCE,
            "configured": bool(self._key.get_secret_value()),
            "network_enabled": self.settings.network_enabled,
            "account": copy.deepcopy(self._account),
            "coverage": copy.deepcopy(self._coverage),
            "last_error": self.last_error,
            "blocked_seasons": sorted(self._blocked_seasons),
            "mapped_competitions": len(self.settings.competitions),
            "mapped_teams": len(self.settings.teams),
            "requests_made_today": self._daily_calls,
            "daily_request_budget": self.settings.daily_request_budget,
            "recent_requests": copy.deepcopy(self._request_log),
            "note": "La copertura dipende dal piano. Nessun dato storico sostituisce quello attuale.",
        }

    def _record_request(self, endpoint, params, *, http_status, result_count, cache_hit):
        self._request_log.append(
            {
                "endpoint": endpoint,
                "params": copy.deepcopy(params),
                "http_status": http_status,
                "result_count": result_count,
                "cache_hit": cache_hit,
            }
        )
        del self._request_log[:-50]

    def _fail(self, code, message):
        self.last_error = message
        return BetterError(code, message, source=SOURCE)

    async def request(self, endpoint, params=None, *, ttl=3600, backfill=False, local_only=False, fresh=False):
        if endpoint not in {"status", "leagues", "standings", "fixtures", "fixtures/lineups",
                            "fixtures/events", "fixtures/statistics", "fixtures/players"}:
            raise ValueError("Unsupported API-Football endpoint")
        params = params or {}
        cache_key = json.dumps([endpoint, params], sort_keys=True)
        async with self._lock:
            now = self.clock()
            if now.date() != self._day:
                self._daily_calls, self._day = 0, now.date()
            if not fresh and self.archive is not None and self.settings.archive_enabled:
                local = self.archive.lookup(endpoint, params, now, ttl)
                if local is not None:
                    self._record_request(endpoint, params, http_status=None,
                                         result_count=len(local["data"]), cache_hit=True)
                    return local
            if local_only or not self.settings.network_enabled:
                raise BetterError(
                    ErrorCode.MISSING_DATA,
                    "Archivio locale: dato assente o scaduto; attendere l'aggiornamento del collector",
                    source="local_archive",
                )
            if params.get("season") in self._blocked_seasons:
                until, message = self._blocked_seasons[params["season"]]
                if now < until:
                    raise self._fail(ErrorCode.MISSING_DATA, message)
                del self._blocked_seasons[params["season"]]
            cached = self._cache.get(cache_key)
            if not fresh and cached and now < cached[0]:
                data = cached[1].get("data")
                self._record_request(
                    endpoint,
                    params,
                    http_status=None,
                    result_count=len(data) if isinstance(data, list) else (1 if data is not None else 0),
                    cache_hit=True,
                )
                return copy.deepcopy(cached[1])
            if self._daily_calls >= self.settings.daily_request_budget:
                raise self._fail(ErrorCode.RATE_LIMIT, "Budget locale API-Football esaurito per oggi")
            if not self._key.get_secret_value():
                raise self._fail(ErrorCode.CONFIGURATION_ERROR, "API_FOOTBALL_API_KEY assente nel file .env")
            if backfill and (self.archive is None or not self.archive.reserve_request(
                now, self.settings.archive_daily_requests, endpoint, params
            )):
                raise self._fail(ErrorCode.RATE_LIMIT, "Budget giornaliero archivio storico esaurito")

            async def fetch():
                self._daily_calls += 1
                try:
                    return await self.client.get(
                        f"{BASE_URL}/{endpoint}",
                        params=params,
                        headers={"x-apisports-key": self._key.get_secret_value()},
                    )
                except httpx.TimeoutException:
                    raise self._fail(ErrorCode.TIMEOUT, "API-Football: tempo di risposta superato") from None
                except httpx.TransportError:
                    raise self._fail(
                        ErrorCode.TRANSIENT_PROVIDER_ERROR, "API-Football non raggiungibile"
                    ) from None

            response = await self.executor.call(fetch)
            if response.status_code != 200:
                self._record_request(
                    endpoint, params, http_status=response.status_code, result_count=None, cache_hit=False
                )
                code = (
                    ErrorCode.RATE_LIMIT
                    if response.status_code == 429
                    else (
                        ErrorCode.TRANSIENT_PROVIDER_ERROR
                        if response.status_code >= 500
                        else ErrorCode.PERMANENT_PROVIDER_ERROR
                    )
                )
                raise self._fail(code, f"API-Football: richiesta respinta (HTTP {response.status_code})")
            try:
                body = response.json()
                if not isinstance(body, dict) or "errors" not in body or "response" not in body:
                    raise ValueError()
            except (ValueError, TypeError):
                self._record_request(endpoint, params, http_status=200, result_count=None, cache_hit=False)
                raise self._fail(ErrorCode.SCHEMA_ERROR, "Risposta API-Football non valida") from None
            errors = body["errors"]
            if errors:
                self._record_request(endpoint, params, http_status=200, result_count=0, cache_hit=False)
                # Do not expose arbitrary upstream strings (which could echo credentials).
                if isinstance(errors, dict) and "plan" in errors:
                    message = (
                        f"Il piano API-Football non consente la stagione {params.get('season', 'richiesta')}."
                    )
                    if "season" in params:
                        self._blocked_seasons[params["season"]] = (now + timedelta(hours=1), message)
                    raise self._fail(ErrorCode.MISSING_DATA, message)
                code = (
                    ErrorCode.RATE_LIMIT
                    if isinstance(errors, dict) and any("limit" in str(k).lower() for k in errors)
                    else ErrorCode.PERMANENT_PROVIDER_ERROR
                )
                raise self._fail(
                    code, "API-Football ha rifiutato la richiesta: verificare chiave, quota e parametri"
                )
            paging = body.get("paging", {"total": 1})
            if not isinstance(paging, dict) or type(paging.get("total")) is not int:
                raise self._fail(ErrorCode.SCHEMA_ERROR, "Paginazione API-Football non valida")
            if paging["total"] > 1:
                raise self._fail(ErrorCode.MISSING_DATA, "Risposta paginata: campione incompleto non ammesso")
            now = self.clock()
            result = {
                "data": body["response"],
                "retrieved_at": now,
                "url": str(response.url),
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
            if endpoint == "fixtures" and "ids" in params:
                expected = {int(value) for value in str(params["ids"]).split("-")}
                data = result["data"]
                if not isinstance(data, list) or len(data) != len(expected) or {
                    row.get("fixture", {}).get("id") for row in data
                } != expected:
                    raise self._fail(ErrorCode.SCHEMA_ERROR, "Archivio: risposta fixture incompleta")
            if self.archive is not None and self.settings.archive_enabled:
                self.archive.save(endpoint, params, result, ttl)
            data = body["response"]
            self._record_request(
                endpoint,
                params,
                http_status=200,
                result_count=len(data) if isinstance(data, list) else (1 if data is not None else 0),
                cache_hit=False,
            )
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (now + timedelta(seconds=ttl), copy.deepcopy(result))
            return result

    async def check_access(self, league: int, season: int):
        if not self.settings.network_enabled:
            return self.describe()
        account = await self.request("status", fresh=True)
        data = account["data"]
        if not isinstance(data, dict):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Stato account API-Football non valido")
        subscription, requests = data.get("subscription", {}), data.get("requests", {})
        self._account = {
            "plan": subscription.get("plan"),
            "active": subscription.get("active"),
            "requests_today": requests.get("current"),
            "daily_limit": requests.get("limit_day"),
        }
        report = {"league": league, "season": season, "accessible": False}
        try:
            result = await self.request("fixtures", {"league": league, "season": season}, fresh=True)
            if not isinstance(result["data"], list):
                raise self._fail(ErrorCode.SCHEMA_ERROR, "Elenco partite API-Football non valido")
            report.update(accessible=True, fixture_count=len(result["data"]))
        except BetterError as error:
            report["error"] = error.detail.model_dump(mode="json")
        self._coverage = report
        return self.describe()

    def _provenance(self, response, field):
        return Provenance(
            source_id=SOURCE,
            source_type=SourceType.API,
            reliability_tier=ReliabilityTier.B,
            retrieved_at=response["retrieved_at"],
            field=field,
            url=response["url"],
            raw_reference="sha256:" + response["sha256"],
            primary_source_id="api-sports",
            notes="Dati raccolti ora; nessuna ricostruzione della disponibilita storica delle informazioni.",
        )

    def _local_analysis(self, binding):
        return bool(self.settings.analysis_archive_only and binding.league in self.settings.archive_leagues
                    and binding.season in self.settings.archive_seasons)

    async def _resolve(self, match_id, *, analysis=False):
        if self.fixtures is None:
            raise self._fail(ErrorCode.CONFIGURATION_ERROR, "Sorgente partite non collegata")
        match = await self.fixtures.get_match(match_id)
        binding = self._competition_binding(match.competition)
        if binding is None:
            raise self._fail(ErrorCode.MISSING_DATA, "Competizione non associata ad API-Football")
        home = self._team_mapping(match.home_team_id)
        away = self._team_mapping(match.away_team_id)
        params = {
            "league": binding.league,
            "season": binding.season,
            "date": match.kickoff.date().isoformat(),
        }
        native_fixture = self._match_mapping(match.match_id)
        if native_fixture:
            params = {"id": native_fixture}
        response = await self.request("fixtures", params, local_only=analysis and self._local_analysis(binding))
        try:
            timed = [
                row
                for row in response["data"]
                if row["league"]["id"] == binding.league
                and row["league"]["season"] == binding.season
                and datetime.fromisoformat(row["fixture"]["date"]) == match.kickoff
            ]
            scored = []
            for row in timed:
                provider_home_id = row["teams"]["home"]["id"]
                provider_away_id = row["teams"]["away"]["id"]
                home_score = (
                    1.0
                    if home is not None and provider_home_id == home
                    else self._identity_name_score(
                        match.home_team, row["teams"]["home"]["name"]
                    )
                )
                away_score = (
                    1.0
                    if away is not None and provider_away_id == away
                    else self._identity_name_score(
                        match.away_team, row["teams"]["away"]["name"]
                    )
                )
                if (
                    home_score >= self.settings.identity_minimum_name_score
                    and away_score >= self.settings.identity_minimum_name_score
                ):
                    scored.append(
                        {
                            "row": row,
                            "home_score": home_score,
                            "away_score": away_score,
                            "score": 0.35 * home_score + 0.35 * away_score + 0.30,
                        }
                    )
            scored.sort(key=lambda item: (-item["score"], item["row"]["fixture"]["id"]))
            mapped = [
                item
                for item in scored
                if (home is None or item["row"]["teams"]["home"]["id"] == home)
                and (away is None or item["row"]["teams"]["away"]["id"] == away)
                and (
                    native_fixture is None
                    or item["row"]["fixture"]["id"] == native_fixture
                )
            ]
            candidates = [item["row"] for item in mapped]
        except (KeyError, TypeError, ValueError):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Identita partita API-Football non valida") from None
        best = mapped[0] if mapped else None
        automatic = bool(
            best
            and best["score"] >= self.settings.identity_automatic_score
            and (
                len(mapped) == 1
                or best["score"] - mapped[1]["score"] >= self.settings.identity_minimum_margin
            )
        )
        if len(candidates) != 1 or not automatic:
            mapped_conflict = len(scored) == 1 and (
                (home is not None and scored[0]["row"]["teams"]["home"]["id"] != home)
                or (away is not None and scored[0]["row"]["teams"]["away"]["id"] != away)
                or (native_fixture is not None and scored[0]["row"]["fixture"]["id"] != native_fixture)
            )
            status = (
                "CONFLICT"
                if mapped_conflict
                else "AMBIGUOUS"
                if len(mapped) > 1 or (mapped and not automatic)
                else "NOT_FOUND"
            )
            evidence = [item["row"] for item in scored] if mapped_conflict else candidates
            self._record_identity_attempt(match, status, response, evidence)
            raise self._fail(
                ErrorCode.MISSING_DATA,
                f"IDENTITY_MAPPING_{status}: nessuna corrispondenza univoca per squadre, "
                "competizione, orientamento home/away e kickoff",
            )
        candidate = candidates[0]
        if native_fixture and candidate["fixture"]["id"] != native_fixture:
            self._record_identity_attempt(match, "CONFLICT", response, candidates)
            raise self._fail(
                ErrorCode.MISSING_DATA,
                "IDENTITY_MAPPING_CONFLICT: il mapping partita persistito non coincide con il provider",
            )
        exact = (
            normalize_team_name(candidate["teams"]["home"]["name"])
            == normalize_team_name(match.home_team)
            and normalize_team_name(candidate["teams"]["away"]["name"])
            == normalize_team_name(match.away_team)
        )
        self._persist_verified_identity(
            match,
            candidate,
            binding,
            response,
            exact=exact,
            confidence=best["score"],
        )
        self._record_identity_attempt(match, "RESOLVED", response, candidates, selected=candidate)
        return match, candidate, binding

    def _record_identity_attempt(self, match, result, response, candidates, *, selected=None):
        if self.repository is None:
            return
        self.repository.record_identity_attempt(
            match_id=match.match_id,
            attempted_at=response["retrieved_at"],
            result=result,
            reason=f"API_FOOTBALL_{result}",
            candidates=[
                {
                    "fixture_id": row["fixture"]["id"],
                    "kickoff": row["fixture"]["date"],
                    "home_team_id": row["teams"]["home"]["id"],
                    "away_team_id": row["teams"]["away"]["id"],
                }
                for row in candidates
            ],
            selected=(
                {"fixture_id": selected["fixture"]["id"], "provider": SOURCE} if selected else None
            ),
        )

    def _persist_verified_identity(self, match, candidate, binding, response, *, exact, confidence):
        if self.repository is None:
            return
        at = response["retrieved_at"]
        method = (
            f"EXACT_NORMALIZED_{NORMALIZATION_VERSION}"
            if exact
            else f"AUTO_VERIFIED_NAME_{NORMALIZATION_VERSION}"
        )
        shared = {
            "provider": SOURCE,
            "status": "VERIFIED_EXACT" if exact else "VERIFIED_MAPPED",
            "method": method,
            "confidence": confidence,
            "verified_at": at,
            "source_url": response["url"],
            "normalization_version": NORMALIZATION_VERSION,
            "provenance": {
                "league": binding.league,
                "season": binding.season,
                "kickoff": candidate["fixture"]["date"],
                "orientation": "HOME_AWAY_EXACT",
            },
            "identity": None,
        }
        mappings = (
            (
                "competition",
                match.competition,
                f"api-football:league:{binding.league}:season:{binding.season}",
                match.competition,
                candidate["league"].get("name") or str(binding.league),
            ),
            (
                "team",
                match.home_team_id,
                f"api-football:team:{candidate['teams']['home']['id']}",
                match.home_team,
                candidate["teams"]["home"]["name"],
            ),
            (
                "team",
                match.away_team_id,
                f"api-football:team:{candidate['teams']['away']['id']}",
                match.away_team,
                candidate["teams"]["away"]["name"],
            ),
            (
                "match",
                match.match_id,
                f"api-football:fixture:{candidate['fixture']['id']}",
                f"{match.home_team} - {match.away_team}",
                f"{candidate['teams']['home']['name']} - {candidate['teams']['away']['name']}",
            ),
        )
        for kind, source_id, target_id, source_name, target_name in mappings:
            self.repository.save_result_identity_mapping(
                kind=kind,
                source_id=source_id,
                target_id=target_id,
                source_name=source_name,
                target_name=target_name,
                **shared,
            )

    @staticmethod
    def _identity_name_score(source_name, provider_name):
        if normalize_team_name(source_name) == normalize_team_name(provider_name):
            return 1.0
        score = name_similarity(source_name, provider_name)
        source_tokens = set(normalized_name(source_name).split())
        provider_tokens = set(normalized_name(provider_name).split())
        shorter = source_tokens if len(source_tokens) <= len(provider_tokens) else provider_tokens
        longer = provider_tokens if shorter is source_tokens else source_tokens
        if (
            shorter
            and shorter < longer
            and shorter.issubset(longer)
            and all(len(token) >= 4 for token in shorter)
        ):
            score = max(score, 0.94)
        return score

    def _competition_binding(self, source_id):
        configured = self.settings.competitions.get(source_id)
        if configured is not None:
            return configured
        mapping = self._persisted_mapping("competition", source_id)
        if mapping is None:
            return None
        found = re.fullmatch(r"api-football:league:(\d+):season:(\d{4})", mapping["target_id"])
        return CompetitionBinding(league=int(found.group(1)), season=int(found.group(2))) if found else None

    def _team_mapping(self, source_id):
        configured = self.settings.teams.get(source_id)
        if configured is not None:
            return configured
        mapping = self._persisted_mapping("team", source_id)
        if mapping is None:
            return None
        found = re.fullmatch(r"api-football:team:(\d+)", mapping["target_id"])
        return int(found.group(1)) if found else None

    def _match_mapping(self, source_id):
        mapping = self._persisted_mapping("match", source_id)
        if mapping is None:
            return None
        found = re.fullmatch(r"api-football:fixture:(\d+)", mapping["target_id"])
        return int(found.group(1)) if found else None

    def _persisted_mapping(self, kind, source_id):
        if self.repository is None:
            return None
        return self.repository.result_identity_mapping(kind, source_id, provider=SOURCE)

    @staticmethod
    def _segment(rows):
        size = len(rows)
        if not size:
            return None
        wins = sum(row.outcome == "W" for row in rows)
        draws = sum(row.outcome == "D" for row in rows)
        return {
            "sample_size": float(size),
            "goals_for_per_match": sum(row.goals_for for row in rows) / size,
            "goals_against_per_match": sum(row.goals_against for row in rows) / size,
            "win_rate": wins / size,
            "draw_rate": draws / size,
            "loss_rate": (size - wins - draws) / size,
            "btts_rate": sum(row.goals_for > 0 and row.goals_against > 0 for row in rows) / size,
            "over_2_5_rate": sum(row.goals_for + row.goals_against > 2 for row in rows) / size,
        }

    async def get_stats(self, match_id, as_of):
        if abs((self.clock() - as_of).total_seconds()) > 300:
            raise self._fail(ErrorCode.MISSING_DATA, "API-Football non ricostruisce snapshot storici as-of")
        match, native, binding = await self._resolve(match_id, analysis=True)
        profiles, sources, cutoffs = [], [], []
        for side, canonical in (("home", match.home_team_id), ("away", match.away_team_id)):
            team_id = native["teams"][side]["id"]
            response = await self.request(
                "fixtures",
                {"league": binding.league, "season": binding.season, "team": team_id, "status": "FT"},
                local_only=self._local_analysis(binding),
            )
            responses = [(response, binding.league, binding.season)]
            current_count = sum(
                r["fixture"]["status"]["short"] == "FT"
                and datetime.fromisoformat(r["fixture"]["date"]) < min(as_of, match.kickoff)
                for r in response["data"]
            )
            # Supplement only from evidence already acquired locally. Never fetch
            # old seasons on the analysis path or relabel them as this season.
            if self.archive and current_count < self.settings.sample_matches:
                for league in self.archive.leagues:
                    prior = self.archive.lookup(
                        "fixtures", {"league": league, "season": binding.season - 1,
                                     "team": team_id, "status": "FT"},
                        as_of, 30 * 86400,
                    )
                    if prior:
                        responses.append((prior, league, binding.season - 1))
            try:
                rows = []
                for row, expected_league, expected_season in (
                    (row, league, season) for document, league, season in responses for row in document["data"]
                ):
                    date = datetime.fromisoformat(row["fixture"]["date"])
                    if date >= min(as_of, match.kickoff):
                        continue
                    if expected_season != binding.season and date < as_of - timedelta(days=400):
                        continue
                    if row["fixture"]["status"]["short"] != "FT" or row["league"]["id"] != expected_league:
                        continue
                    h, a = row["teams"]["home"]["id"], row["teams"]["away"]["id"]
                    if team_id not in (h, a) or h == a or row["league"]["season"] != expected_season:
                        raise ValueError()
                    score = row["score"]["fulltime"]
                    gf, ga = (
                        (score["home"], score["away"]) if h == team_id else (score["away"], score["home"])
                    )
                    if type(gf) is not int or type(ga) is not int or min(gf, ga) < 0:
                        raise ValueError()
                    venue = "HOME" if h == team_id else "AWAY"
                    opponent = row["teams"]["away"] if venue == "HOME" else row["teams"]["home"]
                    rows.append(
                        RecentTeamMatch(
                            fixture_id=f"api-football:fixture:{row['fixture']['id']}",
                            kickoff=date,
                            competition_id=(
                                f"api-football:league:{row['league']['id']}:season:{row['league']['season']}"
                            ),
                            venue=venue,
                            opponent_id=f"api-football:team:{opponent['id']}",
                            opponent_name=opponent["name"],
                            goals_for=gf,
                            goals_against=ga,
                            outcome="W" if gf > ga else "D" if gf == ga else "L",
                        )
                    )
                rows.sort(key=lambda item: item.kickoff, reverse=True)
                if len({r.fixture_id for r in rows}) != len(rows):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise self._fail(
                    ErrorCode.SCHEMA_ERROR, "Storico risultati API-Football non valido"
                ) from None
            if len(rows) < 2:
                raise self._fail(
                    ErrorCode.MISSING_DATA, "Campione stagionale insufficiente per una delle squadre"
                )
            recent = rows[: self.settings.sample_matches]
            segments = {
                name: segment
                for name, segment in (
                    ("last_5", self._segment(recent[:5])),
                    ("last_10", self._segment(recent[:10])),
                    ("season", self._segment([r for r in rows if r.competition_id == f"api-football:league:{binding.league}:season:{binding.season}"])),
                    ("home", self._segment([row for row in rows if row.venue == "HOME"])),
                    ("away", self._segment([row for row in rows if row.venue == "AWAY"])),
                )
                if segment is not None
            }
            profiles.append(
                TeamStats(
                    team_id=canonical,
                    sample_size=len(recent),
                    goals_for_per_match=sum(r.goals_for for r in recent) / len(recent),
                    goals_against_per_match=sum(r.goals_against for r in recent) / len(recent),
                    segments=segments,
                    recent_matches=tuple(recent),
                )
            )
            sources.extend(self._provenance(document, f"{side}.fixture_results") for document, _, _ in responses)
            cutoffs.append(max(r.kickoff for r in recent))
        return StatsSnapshot(
            snapshot_id="api-football:" + ":".join(p.raw_reference for p in sources),
            match_id=match_id,
            home=profiles[0],
            away=profiles[1],
            data_cutoff=max(cutoffs),
            captured_at=max(p.retrieved_at for p in sources),
            provenance=tuple(sources),
        )

    async def get_lineups(self, match_id, as_of):
        if abs((self.clock() - as_of).total_seconds()) > 300:
            raise self._fail(
                ErrorCode.MISSING_DATA, "API-Football non ricostruisce formazioni storiche as-of"
            )
        _, native, binding = await self._resolve(match_id, analysis=True)
        response = await self.request("fixtures/lineups", {"fixture": native["fixture"]["id"]}, ttl=60,
                                      local_only=self._local_analysis(binding))
        try:
            rows = response["data"]
            if rows == []:
                raise self._fail(ErrorCode.MISSING_DATA, "LINEUP_NOT_PUBLISHED_YET")
            profiles = {}
            for row in rows:
                team = row["team"]["id"]
                if team in profiles:
                    raise ValueError()
                profiles[team] = tuple(p["player"]["name"] for p in row["startXI"])
            home = profiles.get(native["teams"]["home"]["id"], ())
            away = profiles.get(native["teams"]["away"]["id"], ())
            if any(not isinstance(p, str) or not p.strip() for p in home + away):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise self._fail(ErrorCode.SCHEMA_ERROR, "Formazioni API-Football non valide") from None
        if (
            len(home) != 11
            or len(away) != 11
            or len(set(home)) != 11
            or len(set(away)) != 11
            or set(home) & set(away)
            or set(profiles) != {native["teams"]["home"]["id"], native["teams"]["away"]["id"]}
        ):
            raise self._fail(ErrorCode.MISSING_DATA, "Formazioni complete non disponibili da API-Football")
        # Aggregator data is not upgraded to OFFICIAL/A without independent official confirmation.
        return LineupSnapshot(
            snapshot_id="api-football:" + response["sha256"],
            match_id=match_id,
            status="PROBABLE_LINEUP",
            home_players=home,
            away_players=away,
            captured_at=response["retrieved_at"],
            provenance=(self._provenance(response, "lineups.unconfirmed_by_official_source"),),
        )
