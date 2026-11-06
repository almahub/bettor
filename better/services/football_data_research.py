"""Budgeted current-statistics research and deterministic raw-match normalization."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from better.domain.common import Contract
from better.domain.enums import ReliabilityTier, SourceType
from better.domain.football_data_research import (
    FootballDataFindingStatus,
    FootballDataResearchRequest,
    FootballDataResearchResult,
    FootballDataRunStatus,
    RawMatchObservation,
)
from better.domain.models import RecentTeamMatch, StatsSnapshot, TeamStats
from better.domain.provenance import Provenance
from better.domain.research import ResearchProviderFailure, ResearchRunStatus
from better.providers.research import OpenAIFootballDataResearchProvider
from better.services.local_environment import environment_value
from better.storage.repository import canonical_hash

FOOTBALL_DATA_FIELDS = (
    "league_table",
    "recent_form",
    "home_away_splits",
    "shots",
    "shots_on_target",
    "possession",
    "clean_sheets",
    "btts",
    "over_under_2_5",
    "current_player_stats",
)


class FootballDataResearchConfiguration(Contract):
    enabled: bool = False
    fields: tuple[str, ...] = FOOTBALL_DATA_FIELDS
    supported_competitions: tuple[str, ...] = ("Serie A", "Serie B", "Serie C")
    max_search_calls: int = Field(default=3, ge=1, le=3)
    max_tokens: int = Field(default=8000, ge=100)
    max_cost_per_match: Decimal = Field(default=Decimal("0.25"), gt=0)
    season_ttl_seconds: int = Field(default=21600, ge=60)
    recent_form_ttl_seconds: int = Field(default=1800, ge=60)

    @classmethod
    def from_env(cls, base=None):
        values = (base or cls()).model_dump()
        if "FOOTBALL_DATA_RESEARCH_ENABLED" in os.environ:
            values["enabled"] = os.environ["FOOTBALL_DATA_RESEARCH_ENABLED"].lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        return cls.model_validate(values)


def build_football_data_research(configuration, repository, metrics, *, provider=None, clock=None):
    configuration = FootballDataResearchConfiguration.from_env(configuration)
    if configuration.enabled and provider is None:
        key = environment_value("OPENAI_API_KEY")
        if key:
            provider = OpenAIFootballDataResearchProvider(
                api_key=key,
                transport="agents_api",
                agent_id=environment_value("OPENAI_FOOTBALL_DATA_AGENT_ID") or None,
                timeout_seconds=float(os.environ.get("OPENAI_RESEARCH_TIMEOUT_SECONDS", "30")),
                max_output_tokens=configuration.max_tokens,
                max_search_calls=configuration.max_search_calls,
                max_cost_per_match=configuration.max_cost_per_match,
                web_search_context_size="low",
                web_search_cost_per_call=Decimal("0.01"),
            )
    return FootballDataResearchService(
        configuration, provider, repository=repository, metrics=metrics, clock=clock
    )


class FootballDataResearchService:
    def __init__(self, settings, provider, *, repository, metrics, clock=None):
        self.settings = settings
        self.provider = provider
        self.repository = repository
        self.metrics = metrics
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cache = {}

    def supports(self, competition):
        lowered = competition.strip().casefold()
        supported = {value.strip().casefold() for value in self.settings.supported_competitions}
        if lowered in supported:
            return True
        return "serie c" in supported and lowered.startswith("serie c girone ")

    def describe(self):
        missing = []
        if self.settings.enabled and self.provider is None:
            missing.append("OPENAI_API_KEY")
        if (
            self.settings.enabled
            and self.provider is not None
            and not getattr(self.provider, "agent_id", None)
        ):
            missing.append("OPENAI_FOOTBALL_DATA_AGENT_ID")
        return {
            "enabled": self.settings.enabled,
            "provider": getattr(self.provider, "provider_id", None),
            "transport": getattr(self.provider, "transport", None),
            "agent_id": getattr(self.provider, "agent_id", None),
            "fields": list(self.settings.fields),
            "supported_competitions": list(self.settings.supported_competitions),
            "ready": not missing,
            "configuration_error": f"Configurazione mancante: {', '.join(missing)}" if missing else None,
        }

    async def research(self, match, as_of, *, requested_fields=None, cache_only=False):
        fields = tuple(field for field in self.settings.fields
                       if requested_fields is None or field in requested_fields)
        if not fields:
            raise ValueError("No configured football-data fields requested")
        now = self.clock()
        if not self.settings.enabled:
            return self._empty(match, as_of, FootballDataRunStatus.DISABLED, now, fields=fields)
        if not self.supports(match.competition):
            return self._empty(
                match,
                as_of,
                FootballDataRunStatus.OUT_OF_SCOPE,
                now,
                "Unsupported competition; Serie A/B/C only",
                fields=fields,
            )
        if self.provider is None:
            return self._empty(
                match,
                as_of,
                FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_ERROR,
                now,
                "OPENAI_API_KEY is not configured",
                fields=fields,
            )
        bucket = int(as_of.timestamp()) // self.settings.recent_form_ttl_seconds
        key = (match.match_id, match.season, bucket, fields)
        if key in self._cache and self._cache[key].as_of <= as_of and self._cache[key].completed_at <= now:
            self.metrics.research_cache(True, category="football_data")
            return self._cache[key].model_copy(update={"cached": True})
        request_id = str(uuid5(NAMESPACE_URL, f"better:football-data:{match.match_id}:{match.season}:{bucket}:{fields}"))
        reusable = next(
            (
                row
                for row in self.repository.list_records(
                    "annotations", last=1000, entity_id="football-data-research:" + match.match_id
                )
                if set(fields) <= set(row.get("requested_fields", ()))
                and row.get("as_of") and datetime.fromisoformat(row["as_of"]) <= as_of
                and 0 <= (now - datetime.fromisoformat(row["completed_at"])).total_seconds() < self.settings.recent_form_ttl_seconds
                and row.get("agent_id") == getattr(self.provider, "agent_id", None)
                and row.get("status") == FootballDataRunStatus.OK.value
            ),
            None,
        )
        if reusable is not None:
            result = FootballDataResearchResult.model_validate(reusable).model_copy(
                update={"cached": True}
            )
            self._cache[key] = result
            self.metrics.research_cache(True, category="football_data")
            return result
        if cache_only:
            return None
        request = FootballDataResearchRequest(
            request_id=request_id,
            match_id=match.match_id,
            home_team=match.home_team,
            away_team=match.away_team,
            competition=match.competition,
            season=match.season,
            kickoff=match.kickoff,
            as_of=as_of,
            requested_fields=fields,
            requested_at=now,
        )
        self.metrics.research_requested(request.requested_fields, category="football_data")
        try:
            result = await self.provider.research(request)
        except ResearchProviderFailure as error:
            mapping = {
                ResearchRunStatus.RESEARCH_TIMEOUT: FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_TIMEOUT,
                ResearchRunStatus.RESEARCH_RATE_LIMIT: FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_RATE_LIMIT,
                ResearchRunStatus.RESEARCH_SCHEMA_ERROR: FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_SCHEMA_ERROR,
            }
            result = self._empty(
                match,
                as_of,
                mapping.get(error.status, FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_ERROR),
                self.clock(),
                str(error),
                fields=fields,
            ).model_copy(update={"request_id": request.request_id})
        if (
            result.search_calls > self.settings.max_search_calls
            or result.input_tokens + result.output_tokens > self.settings.max_tokens
            or (
                result.estimated_cost is not None
                and Decimal(str(result.estimated_cost)) > self.settings.max_cost_per_match
            )
        ):
            result = result.model_copy(
                update={
                    "status": FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_BUDGET_EXCEEDED,
                    "error": "Football-data response exceeded its per-match budget",
                    "unresolved_fields": fields,
                }
            )
        result = result.model_copy(update={"requested_fields": fields})
        self.metrics.research_completed(result, latency_ms=result.duration_ms or 0, category="football_data")
        self.metrics.research_cache(False, category="football_data")
        document = result.model_dump(mode="json")
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "annotations",
                identifier="football-data:" + canonical_hash(document),
                entity_id="football-data-research:" + match.match_id,
                match_id=match.match_id,
                payload=document,
            )
        if result.status == FootballDataRunStatus.OK:
            self._cache[key] = result
        return result

    def _empty(self, match, as_of, status, completed_at, error=None, *, fields=None):
        return FootballDataResearchResult(
            request_id="none:" + canonical_hash({"match": match.match_id, "status": status}),
            match_id=match.match_id,
            as_of=as_of,
            requested_fields=fields or self.settings.fields,
            unresolved_fields=fields or self.settings.fields,
            agent_id=getattr(self.provider, "agent_id", None),
            status=status,
            error=error,
            completed_at=completed_at,
        )

    async def close(self):
        if self.provider is not None and hasattr(self.provider, "close"):
            await self.provider.close()


def _provenance(source, field):
    return Provenance(
        source_id=source.publisher,
        source_type=SourceType.OFFICIAL if source.source_tier == ReliabilityTier.A else SourceType.MEDIA,
        url=source.url,
        retrieved_at=source.retrieved_at,
        published_at=source.published_at or source.updated_at,
        reliability_tier=source.source_tier,
        field=field,
        raw_reference=source.url,
        primary_source_id=source.lineage[0] if source.lineage else source.url,
        notes=f"season={source.season}; sample_size={source.sample_size}; family={source.source_family}",
    )


def _metrics(matches):
    count = len(matches)
    gf = sum(match.goals_for for match in matches)
    ga = sum(match.goals_against for match in matches)
    return {
        "matches": count,
        "wins": sum(match.goals_for > match.goals_against for match in matches),
        "draws": sum(match.goals_for == match.goals_against for match in matches),
        "losses": sum(match.goals_for < match.goals_against for match in matches),
        "goals_for": gf,
        "goals_against": ga,
        "goals_for_per_match": gf / count if count else 0,
        "goals_against_per_match": ga / count if count else 0,
        "btts_rate": sum(match.goals_for > 0 and match.goals_against > 0 for match in matches) / count
        if count
        else 0,
        "over_2_5_rate": sum(match.goals_for + match.goals_against > 2 for match in matches) / count
        if count
        else 0,
    }


def missing_model_research_fields(snapshot, minimum):
    """Only recover observations the current statistics adapter can consume.

    Opponent strength / tactical traits are not researchable quantitative fields.
    A recent-form list also supplies the missing venue splits deterministically.
    """
    if snapshot is None:
        return ("recent_form",)
    if any(team.sample_size < minimum or not {"last_5", "last_10", "home", "away"} <= team.segments.keys()
           for team in (snapshot.home, snapshot.away)):
        return ("recent_form",)
    return ()


def merge_football_stats(existing, recovered):
    if recovered is None:
        return existing
    if existing is None:
        return recovered
    changed = False
    teams = {}
    for side in ("home", "away"):
        original, extra = getattr(existing, side), getattr(recovered, side)
        if original.team_id != extra.team_id:
            return existing
        known = {(r.kickoff, r.opponent_name.casefold(), r.venue): (r.goals_for, r.goals_against)
                 for r in original.recent_matches}
        if any((r.kickoff, r.opponent_name.casefold(), r.venue) in known
               and known[r.kickoff, r.opponent_name.casefold(), r.venue] != (r.goals_for, r.goals_against)
               for r in extra.recent_matches):
            return existing
        if extra.sample_size > original.sample_size:
            changed = True
            teams[side] = original.model_copy(update={
                "sample_size": extra.sample_size, "goals_for_per_match": extra.goals_for_per_match,
                "goals_against_per_match": extra.goals_against_per_match,
                "recent_matches": extra.recent_matches,
                "segments": {**original.segments, **extra.segments},
            })
        else:
            missing = {k: v for k, v in extra.segments.items() if k not in original.segments}
            changed |= bool(missing)
            teams[side] = original.model_copy(update={"segments": {**original.segments, **missing}})
    if not changed:
        return existing
    return existing.model_copy(update={
        **teams, "snapshot_id": existing.snapshot_id + ":" + recovered.snapshot_id,
        "captured_at": max(existing.captured_at, recovered.captured_at),
        "data_cutoff": max(existing.data_cutoff, recovered.data_cutoff),
        "provenance": tuple(dict.fromkeys((*existing.provenance, *recovered.provenance))),
    })


def normalize_football_data(match, result):
    finding = next(
        (
            item
            for item in result.findings
            if item.field == "recent_form" and item.status == FootballDataFindingStatus.FOUND
        ),
        None,
    )
    if finding is None or not isinstance(finding.value, dict):
        return None
    cutoff = result.as_of
    oldest = cutoff - timedelta(days=400)

    def observations(side):
        raw = finding.value.get(side, [])
        valid = []
        seen = {}
        for item in raw if isinstance(raw, list) else []:
            try:
                observation = RawMatchObservation.model_validate(item)
            except ValueError:
                continue
            if oldest <= observation.date < cutoff:
                key = (observation.date, observation.opponent.casefold(), observation.venue)
                if key in seen:
                    if seen[key] != observation:
                        return ()
                    continue
                seen[key] = observation
                valid.append(observation)
        return tuple(sorted(valid, key=lambda item: item.date, reverse=True)[:10])

    home_matches, away_matches = observations("home"), observations("away")
    if not home_matches or not away_matches:
        return None
    provenance = tuple(_provenance(source, "recent_form") for source in finding.sources)

    def team(team_id, matches):
        recent = _metrics(matches)
        home = _metrics(tuple(item for item in matches if item.venue == "HOME"))
        away = _metrics(tuple(item for item in matches if item.venue == "AWAY"))
        return TeamStats(
            team_id=team_id,
            sample_size=len(matches),
            goals_for_per_match=recent["goals_for_per_match"],
            goals_against_per_match=recent["goals_against_per_match"],
            recent_matches=tuple(
                RecentTeamMatch(
                    fixture_id="research:" + canonical_hash({"team": team_id, "date": item.date.isoformat(), "opponent": item.opponent, "competition": item.competition}),
                    kickoff=item.date, competition_id=item.competition, venue=item.venue,
                    opponent_id="research-name:" + item.opponent, opponent_name=item.opponent,
                    goals_for=item.goals_for, goals_against=item.goals_against,
                    outcome="W" if item.goals_for > item.goals_against else "L" if item.goals_for < item.goals_against else "D",
                ) for item in matches if item.venue in {"HOME", "AWAY"}
            ),
            segments={
                "last_5": _metrics(matches[:5]),
                "last_10": recent,
                "home": home,
                "away": away,
            },
        )

    return StatsSnapshot(
        snapshot_id="football-data:" + canonical_hash(result.model_dump(mode="json")),
        match_id=match.match_id,
        home=team(match.home_team_id, home_matches),
        away=team(match.away_team_id, away_matches),
        data_cutoff=max(item.date for item in (*home_matches, *away_matches)),
        captured_at=result.completed_at,
        provenance=provenance,
    )
