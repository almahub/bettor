"""Composition root: only this boundary selects concrete providers."""

from better.config import Settings
from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import make_demo_bundle
from better.storage.repository import Repository
from better.system_one import ShadowCoordinator, SystemOneSettings


class UnconfiguredProvider:
    async def list_matches(self, as_of):
        return ()

    async def get_match(self, match_id):
        raise BetterError(
            ErrorCode.MISSING_DATA, "No fixture source configured; use an explicit demo or supply providers"
        )

    async def get_stats(self, match_id, as_of):
        raise BetterError(ErrorCode.MISSING_DATA, "No statistics provider configured")

    async def get_odds(self, match_id, as_of):
        raise BetterError(ErrorCode.MISSING_DATA, "No odds provider configured")

    async def get_news(self, match_id, as_of):
        raise BetterError(ErrorCode.MISSING_DATA, "No news provider configured")

    async def get_lineups(self, match_id, as_of):
        raise BetterError(ErrorCode.MISSING_DATA, "No lineup provider configured")


def make_application(
    *,
    settings=None,
    providers=None,
    repository=None,
    demo=None,
    as_of=None,
    system_one_settings=None,
    system_one_provider=None,
    system_two=None,
    external_research=None,
    external_research_provider=None,
    football_data_research=None,
    football_data_research_provider=None,
):
    settings = settings or Settings.load()
    repository = repository or Repository(settings.database_url)
    if providers is None:
        if demo:
            providers = make_demo_bundle(demo, as_of=as_of)
        else:
            source = UnconfiguredProvider()
            from better.services.provider_factory import build_providers

            providers = build_providers(settings, source, repository=repository)
    from better.observability.metrics import Metrics
    from better.services.external_research import build_external_research
    from better.services.provider_factory import SourceConfiguration

    metrics = getattr(external_research, "metrics", None) or Metrics()
    if external_research is None:
        source_config = SourceConfiguration.load(settings.sources_config)
        external_research = build_external_research(
            source_config.external_research,
            repository,
            metrics,
            provider=external_research_provider,
        )
    if football_data_research is None:
        from better.services.football_data_research import build_football_data_research

        source_config = SourceConfiguration.load(settings.sources_config)
        football_data_research = build_football_data_research(
            source_config.football_data_research,
            repository,
            metrics,
            provider=football_data_research_provider,
        )
    external_agent_id = getattr(getattr(external_research, "provider", None), "agent_id", None)
    football_agent_id = getattr(getattr(football_data_research, "provider", None), "agent_id", None)
    if external_agent_id and external_agent_id == football_agent_id:
        raise ValueError(
            "OPENAI_RESEARCH_AGENT_ID and OPENAI_FOOTBALL_DATA_AGENT_ID must reference "
            "two distinct saved agents. Remove stale PowerShell variables or set distinct values "
            "in the local .env file"
        )
    return Orchestrator(
        providers,
        settings,
        repository,
        shadow=ShadowCoordinator(system_one_settings or SystemOneSettings.from_env(), system_one_provider),
        system_two=system_two,
        metrics=metrics,
        external_research=external_research,
        football_data_research=football_data_research,
    )
