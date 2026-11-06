import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, TypeAdapter

from better.domain.common import Contract
from better.domain.models import HistoricalPrediction, Portfolio
from better.providers.api_football import APIFootballSettings
from better.providers.base import ProviderBundle
from better.providers.free_football import FreeFootballSettings
from better.providers.http_feed import HTTPFootballProvider
from better.providers.sport_888 import Sport888Settings
from better.providers.sportradar import SportradarSettings
from better.services.external_research import ExternalResearchConfiguration
from better.services.football_data_research import FootballDataResearchConfiguration
from better.services.local_environment import environment_value

SourceName = Literal["none", "http_feed", "football_data_org", "sport_888", "api_football", "sportradar", "free_football"]


class SourcePolicy(Contract):
    primary: SourceName = "none"
    fallback: SourceName | None = None
    official_first: bool = True


class FeedSettings(Contract):
    base_url_env: str = "BETTER_FEED_BASE_URL"
    api_key_env: str = "BETTER_FEED_API_KEY"
    provider_id: str = "configured-football-feed"


class SourceConfiguration(Contract):
    providers: dict[Literal["fixture", "football_stats", "odds", "news", "lineups"], SourcePolicy] = Field(
        default_factory=dict
    )
    http_feed: FeedSettings = Field(default_factory=FeedSettings)
    sport_888: Sport888Settings = Field(default_factory=Sport888Settings)
    api_football: APIFootballSettings = Field(default_factory=APIFootballSettings)
    free_football: FreeFootballSettings = Field(default_factory=FreeFootballSettings)
    football_data_org_competitions: dict[str, str] = Field(default_factory=dict)
    sportradar: SportradarSettings = Field(default_factory=SportradarSettings)
    external_research: ExternalResearchConfiguration = Field(default_factory=ExternalResearchConfiguration)
    football_data_research: FootballDataResearchConfiguration = Field(
        default_factory=FootballDataResearchConfiguration
    )

    @classmethod
    def load(cls, path):
        path = Path(path)
        return (
            cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
            if path.exists()
            else cls()
        )


def build_providers(settings, missing, *, repository=None):
    config = SourceConfiguration.load(settings.sources_config)
    sources = {"none": missing}

    def resolve(name):
        if name in sources:
            return sources[name]
        if name == "http_feed":
            base_url = os.environ.get(config.http_feed.base_url_env)
            if not base_url:
                raise ValueError(f"http_feed requires environment variable {config.http_feed.base_url_env}")
            sources[name] = HTTPFootballProvider(
                base_url,
                config.http_feed.provider_id,
                api_key=os.environ.get(config.http_feed.api_key_env),
                timeout=settings.provider_timeout_seconds,
                retries=settings.provider_retries,
            )
        elif name == "football_data_org":
            from better.providers.football_data_org import FootballDataOrgFixtureProvider

            sources[name] = FootballDataOrgFixtureProvider()
        elif name == "sport_888":
            from better.providers.sport_888 import Sport888Provider

            sources[name] = Sport888Provider(config.sport_888)
        elif name == "api_football":
            from better.providers.api_football import APIFootballProvider

            sources[name] = APIFootballProvider(
                config.api_football,
                fixtures=bindings.get("fixtures"),
                repository=repository,
            )
        elif name == "free_football":
            from better.providers.free_football import FreeFootballProvider
            if repository is None:
                raise ValueError("free_football requires a persistent repository")
            sources[name] = FreeFootballProvider(config.free_football, repository=repository,
                fixtures=bindings.get("fixtures"), archived=resolve("api_football"))
        elif name == "sportradar":
            from better.providers.sportradar import SportradarSoccerProvider

            sources[name] = SportradarSoccerProvider(
                config.sportradar, fixtures=bindings.get("fixtures"), repository=repository
            )
        return sources[name]

    bindings = {}
    for key, attr in (
        ("fixture", "fixtures"),
        ("football_stats", "stats"),
        ("odds", "odds"),
        ("news", "news"),
        ("lineups", "lineups"),
    ):
        policy = config.providers.get(key, SourcePolicy())
        if key not in {"football_stats", "lineups"} and "api_football" in {policy.primary, policy.fallback}:
            raise ValueError("api_football supplies statistics and lineups only in the analysis pipeline")
        if key not in {"football_stats", "lineups"} and "sportradar" in {policy.primary, policy.fallback}:
            raise ValueError("sportradar supplies statistics and lineups only in the analysis pipeline")
        if key not in {"fixture", "odds"} and "sport_888" in {policy.primary, policy.fallback}:
            raise ValueError("sport_888 supplies fixtures and odds only, not statistics/news/lineups")
        if key != "fixture" and "football_data_org" in {policy.primary, policy.fallback}:
            raise ValueError(
                "football_data_org adapter supplies fixtures only, not statistics/odds/news/lineups"
            )
        primary = resolve(policy.primary)
        if policy.fallback:
            from better.providers.multi_source import MultiSourceProvider

            primary = MultiSourceProvider(primary, resolve(policy.fallback))
        bindings[attr] = primary
    history = ()
    portfolio = None
    if history_path := os.environ.get("BETTER_HISTORY_FILE"):
        history = TypeAdapter(tuple[HistoricalPrediction, ...]).validate_json(
            Path(history_path).read_text(encoding="utf-8")
        )
    if portfolio_path := os.environ.get("BETTER_PORTFOLIO_FILE"):
        portfolio = Portfolio.model_validate(json.loads(Path(portfolio_path).read_text(encoding="utf-8")))
    results = None
    fixture_source = bindings.get("fixtures")
    try:
        from better.providers.api_football_results import APIFootballResultProvider
        from better.providers.football_data_org import FootballDataOrgFixtureProvider
        from better.providers.results import CompositeResultProvider, FootballDataOrgResultProvider

        result_sources = []
        api_was_bound = "api_football" in sources
        api_transport = sources.get("api_football")
        if api_transport is None and config.api_football.competitions:
            api_transport = resolve("api_football")
        if api_transport is not None and config.api_football.results_enabled and api_transport.describe()["configured"]:
            result_sources.append(APIFootballResultProvider(api_transport, owns_transport=not api_was_bound))
        if isinstance(fixture_source, FootballDataOrgFixtureProvider):
            result_sources.append(FootballDataOrgResultProvider(fixture_source))
        if not isinstance(fixture_source, FootballDataOrgFixtureProvider) and environment_value("FOOTBALL_DATA_ORG_API_KEY"):
            # Result identity is independent from the configured pre-match fixture source.
            result_sources.append(
                FootballDataOrgResultProvider(FootballDataOrgFixtureProvider(), owns_fixture_provider=True,
                                             competitions=config.football_data_org_competitions)
            )
        if config.free_football.enabled:
            free = resolve("free_football")
            free.football_data_org = next((p for p in result_sources if isinstance(p, FootballDataOrgResultProvider)), None)
            result_sources.append(free)
        if result_sources:
            results = CompositeResultProvider(*result_sources)
    except ImportError:
        pass
    return ProviderBundle(
        **bindings,
        validation_history=history,
        portfolio=portfolio,
        results=results,
    )
