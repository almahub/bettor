"""Same-origin dashboard and thin presentation endpoints. No decision rules live here."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, field_validator

from better.agents.registry import AGENT_REGISTRY
from better.domain.common import Contract
from better.domain.enums import AnalysisDepth, MarketType
from better.domain.schemas import AnalysisRequest, AnalysisRun
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import DEMO_CASES, FakeProvider, make_demo_bundle
from better.services.bank_service import (
    BankBet,
    BankBetCancellation,
    BankInitialize,
    BankService,
    BankSettlement,
    BankTransfer,
)
from better.storage.repository import canonical_hash

WEB_ROOT = Path(__file__).parents[1] / "web"


class JournalRemoval(Contract):
    analysis_ids: list[UUID] = Field(min_length=1, max_length=100)


class BatchAnalysisRequest(Contract):
    mode: Literal["ALL", "SELECTION"]
    match_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    filter: dict = Field(default_factory=dict)


class DashboardStaticFiles(StaticFiles):
    """Serve ES modules independently of Windows registry MIME associations."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith((".mjs", ".js")) and response.status_code == 200:
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        response.headers["cache-control"] = "no-cache"
        response.headers["x-content-type-options"] = "nosniff"
        return response


class DemoAnalysisRequest(Contract):
    scenario: str = "bet"
    markets: tuple[MarketType, ...] = (MarketType.MATCH_WINNER, MarketType.OVER_UNDER_2_5, MarketType.BTTS)
    depth: AnalysisDepth = AnalysisDepth.FULL

    @field_validator("scenario")
    @classmethod
    def known_scenario(cls, value):
        if value not in DEMO_CASES:
            raise ValueError("Unknown synthetic scenario")
        return value


class FootballCSVImport(Contract):
    csv_text: str = Field(min_length=1, max_length=2_000_000)


def mount_dashboard(app, runtime, check_rate_limit):
    app.mount("/assets", DashboardStaticFiles(directory=WEB_ROOT), name="dashboard-assets")

    @app.get("/matches/{match_id}/research")
    async def match_research(match_id: str, request: Request):
        check_rate_limit(request)
        from better.services.match_research import MatchResearchService

        if getattr(app.state, "match_research", None) is None:
            app.state.match_research = MatchResearchService(runtime())
        return await app.state.match_research.inspect(match_id)

    def bank():
        return BankService(runtime().repository)

    def find_provider(kind):
        provider = runtime().providers.stats
        candidates = (provider.primary, provider.secondary) if hasattr(provider, "primary") else (provider,)
        candidates = (*candidates, *(p.archived for p in candidates if getattr(p, "archived", None) is not None))
        return next((candidate for candidate in candidates if isinstance(candidate, kind)), None)

    def bank_write(request):
        check_rate_limit(request)
        if (
            isinstance(runtime().providers.fixtures, FakeProvider)
            or runtime().providers.portfolio is not None
        ):
            raise HTTPException(409, "Banca modificabile solo nel workspace reale senza portafoglio esterno")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")

    @app.get("/bank")
    async def bank_status():
        from better.services.automation_control import AutomationControl
        return {**bank().status(), "research_budget": AutomationControl(runtime().repository, runtime().settings).report()}

    from better.services.automation_control import AutomationControl, AutomationSwitch, ResearchLimits

    @app.post("/bank/research-budget")
    async def research_budget_update(payload: ResearchLimits, request: Request):
        bank_write(request)
        try:
            return AutomationControl(runtime().repository, runtime().settings).update(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/automation")
    async def automation_switch(payload: AutomationSwitch, request: Request):
        bank_write(request)
        try:
            return AutomationControl(runtime().repository, runtime().settings).update(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/bank/initialize")
    async def bank_initialize(payload: BankInitialize, request: Request):
        bank_write(request)
        try:
            return bank().initialize(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/bank/bets")
    async def bank_bet(payload: BankBet, request: Request):
        bank_write(request)
        match = await runtime().providers.fixtures.get_match(payload.match_id)
        try:
            return bank().record_bet(payload, match)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/bank/bets/{bet_id}/settle")
    async def bank_settle(bet_id: str, payload: BankSettlement, request: Request):
        bank_write(request)
        try:
            return bank().settle(bet_id, payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/bank/deposit")
    async def bank_deposit(payload: BankTransfer, request: Request):
        bank_write(request)
        try:
            return bank().deposit(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/bank/withdraw")
    async def bank_withdraw(payload: BankTransfer, request: Request):
        bank_write(request)
        try:
            return bank().withdraw(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/bank/bets/{bet_id}/remove")
    async def bank_remove_manual_bet(
        bet_id: str, payload: BankBetCancellation, request: Request
    ):
        bank_write(request)
        try:
            return bank().cancel_manual_bet(bet_id, payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/archive/football")
    async def football_archive():
        from better.providers.api_football import APIFootballProvider
        provider = find_provider(APIFootballProvider)
        if provider is None or provider.archive is None:
            raise HTTPException(409, "Archivio calcio non configurato")
        return {"enabled": provider.settings.archive_enabled,
                "competitions": [provider.archive.report(datetime.now(timezone.utc), league)
                                 for league in provider.settings.archive_leagues]}

    @app.post("/archive/football/import-csv")
    async def import_football_csv(payload: FootballCSVImport, request: Request):
        bank_write(request)
        from better.providers.free_football import FreeFootballProvider, parse_csv
        provider = find_provider(FreeFootballProvider)
        if provider is None:
            raise HTTPException(409, "Import CSV non configurato")
        try:
            rows = parse_csv(payload.csv_text)
            provider.save_rows(rows, "https://bettor.local/import-csv", datetime.now(timezone.utc))
            return {"imported": len(rows), "validation_observations_created": 0}
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/archive/football/standings")
    async def football_standings():
        repo = runtime().repository
        checks = repo.list_records("annotations", last=1, entity_id="football-data:standings:football-data:competition:2019:checks")
        if not checks:
            return {"status": "NOT_AVAILABLE", "reason": "FOOTBALL_DATA_ORG_API_KEY richiesta; classifiche non ancora acquisite"}
        content = repo.all_records("annotations", entity_id="football-data:standings:football-data:competition:2019")
        matching = next((r for r in content if "football-data:standings-content:" + canonical_hash(r) == checks[0]["content_id"]), None)
        return {"status": "AVAILABLE", "acquired_at": checks[0]["acquired_at"], "data": matching}

    @app.get("/archive/football/fixtures/{fixture_id}")
    async def football_archive_fixture(fixture_id: int, as_of: datetime | None = None):
        from better.providers.api_football import APIFootballProvider
        provider = find_provider(APIFootballProvider)
        if as_of is not None and as_of.tzinfo is None:
            raise HTTPException(422, "as_of deve includere il fuso orario")
        result = provider.archive.fixture(fixture_id, as_of=as_of) if provider and provider.archive else None
        if result is None:
            raise HTTPException(404, "Partita non presente nell'archivio alla data richiesta")
        return result

    @app.get("/archive/serie-a")
    async def serie_a_archive():
        from better.providers.api_football import APIFootballProvider
        from better.services.football_archive import FootballArchive

        provider = find_provider(APIFootballProvider)
        archive = provider.archive if provider and provider.archive else FootballArchive(runtime().repository)
        return {"enabled": bool(provider and provider.settings.archive_enabled),
                **archive.report(datetime.now(timezone.utc))}

    @app.get("/archive/serie-a/fixtures/{fixture_id}")
    async def archived_fixture(fixture_id: int, as_of: datetime | None = None):
        from better.services.football_archive import FootballArchive

        if as_of is not None and as_of.tzinfo is None:
            raise HTTPException(422, "as_of deve includere il fuso orario")
        result = FootballArchive(runtime().repository).fixture(fixture_id, as_of=as_of)
        if result is None:
            raise HTTPException(404, "Partita non presente nell'archivio alla data richiesta")
        return result

    @app.post("/sources/api-football/check")
    async def check_api_football(request: Request):
        check_rate_limit(request)
        from better.providers.api_football import APIFootballProvider

        provider = find_provider(APIFootballProvider)
        if provider is None:
            raise HTTPException(409, "API-Football non configurato in questo workspace")
        binding = next(iter(provider.settings.competitions.values()), None)
        if binding is None:
            raise HTTPException(409, "Nessuna competizione associata: usare sources-check da terminale")
        return await provider.check_access(binding.league, binding.season)

    @app.post("/sources/sportradar/check")
    async def check_sportradar(request: Request):
        check_rate_limit(request)
        from better.providers.sportradar import SportradarSoccerProvider

        provider = find_provider(SportradarSoccerProvider)
        if provider is None:
            raise HTTPException(409, "Sportradar non configurato in questo workspace")
        return await provider.check_access()

    @app.get("/sources/sportradar/identity-coverage")
    async def sportradar_identity_coverage():
        from better.providers.sportradar import SportradarSoccerProvider

        provider = find_provider(SportradarSoccerProvider)
        if provider is None:
            raise HTTPException(409, "Sportradar non configurato in questo workspace")
        return runtime().repository.identity_coverage()

    @app.get("/history/collection")
    async def historical_collection():
        return runtime().repository.historical_collection_status()

    @app.get("/data-pipeline")
    async def data_pipeline():
        from better.services.data_pipeline import DataPipelineService

        report = DataPipelineService(runtime().repository, settings=runtime().settings).report()
        collector = getattr(app.state, "data_pipeline_collector", None)
        report["collector"] = collector.status() if collector else {"status": "STOPPED", "active": False, "healthy": False}
        return report

    @app.post("/data-pipeline/collect-due")
    async def data_pipeline_collect_due(request: Request):
        check_rate_limit(request)
        collector = getattr(request.app.state, "data_pipeline_collector", None)
        if collector is None:
            from better.services.data_pipeline import DataPipelineCollector

            collector = DataPipelineCollector(runtime())
        return await collector.collect_due()

    @app.post("/data-pipeline/results")
    async def data_pipeline_result(payload: dict, request: Request):
        from better.services.settlement_service import MatchResultInput, SettlementService

        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")
        try:
            validated = MatchResultInput.model_validate(payload)
            return SettlementService(runtime().repository).record(validated)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/data-pipeline/result-identities/manual")
    async def data_pipeline_manual_result_identity(payload: dict, request: Request):
        from datetime import datetime, timezone

        from better.providers.result_identity import (
            DeterministicResultIdentityResolver,
            ManualResultIdentityMapping,
        )

        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")
        try:
            validated = ManualResultIdentityMapping.model_validate(payload)
            resolver = DeterministicResultIdentityResolver(
                runtime().repository,
                None,
                kickoff_tolerance_minutes=runtime().settings.result_identity_kickoff_tolerance_minutes,
                require_competition=runtime().settings.result_identity_require_competition,
                provider_priority=runtime().settings.result_provider_priority,
            )
            return resolver.manual_verify(validated, datetime.now(timezone.utc))
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api-control")
    async def api_control():
        from better.services.api_control import APIControlService

        if getattr(app.state, "api_control", None) is None:
            app.state.api_control = APIControlService(repository=runtime().repository)
        return await app.state.api_control.report()

    @app.post("/api-control/openai/history/archive")
    async def archive_openai_history(request: Request):
        from better.services.api_control import APIControlService

        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")
        if getattr(app.state, "api_control", None) is None:
            app.state.api_control = APIControlService(repository=runtime().repository)
        return app.state.api_control.archive_local_history()

    @app.post("/batch-analysis")
    async def batch_analysis(payload: BatchAnalysisRequest, request: Request):
        from better.services.batch_analysis import BatchAnalysisService

        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")
        return await BatchAnalysisService(runtime()).run(
            payload.match_ids,
            filter_used={**payload.filter, "mode": payload.mode},
        )

    @app.get("/batch-analysis/{batch_id}")
    async def batch_analysis_report(batch_id: UUID):
        from better.services.batch_analysis import BatchAnalysisService

        try:
            return BatchAnalysisService(runtime()).get(batch_id)
        except KeyError as error:
            raise HTTPException(404, "Batch non trovato") from error

    @app.post("/batch-analysis/{batch_id}/refresh")
    async def refresh_batch_analysis(batch_id: UUID, request: Request):
        from better.services.batch_analysis import BatchAnalysisService

        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")
        service = BatchAnalysisService(runtime())
        try:
            previous = service.get(batch_id)
        except KeyError as error:
            raise HTTPException(404, "Batch non trovato") from error
        match_ids = tuple(
            dict.fromkeys(
                row["match_id"]
                for row in previous.get("rows", ())
                if row.get("outcome") == "TRANSFERRED"
            )
        )
        if not match_ids:
            raise HTTPException(409, "Il batch non contiene candidate da rivalutare")
        return await service.run(
            match_ids,
            filter_used={**previous.get("filter", {}), "mode": "REFRESH"},
            parent_batch_id=batch_id,
        )

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(
            WEB_ROOT / "index.html",
            headers={
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            },
        )

    @app.get("/system/ai-agents")
    async def ai_agents():
        from better.services.ai_agents_status import ai_agents_report

        return ai_agents_report(runtime())

    @app.get("/workspace")
    async def workspace():
        engine = runtime()
        from better.agents.catalog import AGENT_CATALOG

        is_demo = isinstance(engine.providers.fixtures, FakeProvider)
        from better.providers.sport_888 import Sport888Provider
        from better.services.application import UnconfiguredProvider

        is_888 = isinstance(engine.providers.fixtures, Sport888Provider)
        from better.providers.api_football import APIFootballProvider
        from better.providers.sportradar import SportradarSoccerProvider

        sportradar = find_provider(SportradarSoccerProvider)
        api_football = find_provider(APIFootballProvider)
        from better.providers.free_football import FreeFootballProvider
        from better.services.local_environment import environment_value
        free = find_provider(FreeFootballProvider)
        standings_checks = engine.repository.list_records(
            "annotations", last=1,
            entity_id="football-data:standings:football-data:competition:2019:checks",
        )
        standings_check = next(iter(standings_checks), None)
        return {
            "is_demo": is_demo,
            "statistics_source": type(engine.providers.stats).__name__,
            "sportradar": sportradar.describe() if sportradar else None,
            "api_football": api_football.describe() if api_football else None,
            "football_data_org": {
                "configured": bool(environment_value("FOOTBALL_DATA_ORG_API_KEY")),
                "status": "ACQUIRED" if standings_check else "NOT_VERIFIED",
                "competition": "Serie A", "competition_id": 2019, "code": "SA",
                "acquired_at": standings_check.get("acquired_at") if standings_check else None,
                "source_url": standings_check.get("source_url") if standings_check else None,
            },
            "free_football": {
                "enabled": True, "competitions": list(free.settings.competitions.values()),
                "football_data_org_configured": bool(environment_value("FOOTBALL_DATA_ORG_API_KEY")),
                "openfootball_divisions": list(free.settings.openfootball_divisions),
                "csv_files": len(free.settings.csv_files),
                "last_refresh": free.last_refresh or next(iter(engine.repository.list_records("annotations", last=1, entity_id="free-football:refresh")), None),
            } if free else None,
            "fixture_source": "888sport_it" if is_888 else "synthetic" if is_demo else "configured",
            "catalog_hours": engine.providers.fixtures.settings.catalog_hours if is_888 else None,
            "competition_names": engine.providers.fixtures.competition_names() if is_888 else {},
            "missing_sources": [
                name
                for name in ("stats", "news", "lineups")
                if isinstance(getattr(engine.providers, name), UnconfiguredProvider)
            ],
            "demo_cases": list(DEMO_CASES) if is_demo else [],
            "supported_markets": list(engine.settings.supported_markets),
            "system_two_enabled": engine.settings.system_two_enabled,
            "external_research": engine.external_research.describe()
            if engine.external_research is not None
            else {"enabled": False, "provider": None, "model": None, "fields": {}},
            "football_data_research": engine.football_data_research.describe()
            if engine.football_data_research is not None
            else {"enabled": False, "provider": None, "fields": []},
            "agents": [
                {
                    "id": identifier,
                    "enabled": engine.settings.agents_enabled.get(identifier, True),
                    "critical": identifier in engine.settings.critical_agents,
                    "dependencies": list(agent.dependencies),
                    "separate_workflow": identifier == "matched_betting",
                    "responsibility": AGENT_CATALOG[identifier]["responsibility"],
                    "data_inputs": list(AGENT_CATALOG[identifier]["data_inputs"]),
                    "output": AGENT_CATALOG[identifier]["output"],
                    "allowed_tools": sorted(agent.allowed_tools),
                }
                for identifier, agent in AGENT_REGISTRY.items()
            ],
        }

    @app.get("/analyses")
    async def analyses(last: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        summaries = []
        for payload in runtime().repository.list_visible_runs(last=last, offset=offset):
            run = AnalysisRun.model_validate(payload)
            match = run.snapshot.match
            summaries.append(
                {
                    "analysis_id": str(run.analysis_id),
                    "match_id": run.match_id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "competition": match.competition,
                    "as_of": run.as_of,
                    "kickoff": match.kickoff,
                    "status": run.decision.status,
                    "market": run.decision.market,
                    "current_odds": run.decision.current_odds,
                    "expected_value": run.decision.expected_value,
                    "estimated_edge": run.decision.estimated_edge,
                    "stake": run.decision.stake,
                    "confidence": run.decision.confidence,
                    "depth": run.depth,
                    "parent_analysis_id": run.parent_analysis_id,
                    "is_demo": any(
                        p.source_id == "synthetic-fixture"
                        for result in run.results.values()
                        for p in result.provenance
                    ),
                }
            )
        return summaries

    @app.post("/analyses/remove")
    async def remove_analyses(payload: JournalRemoval, request: Request):
        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")
        try:
            return {"removed": runtime().repository.hide_runs(payload.analysis_ids)}
        except KeyError as error:
            raise HTTPException(404, "Analisi non trovata: nessuna rimozione effettuata") from error

    @app.post("/demo/analysis", response_model=AnalysisRun)
    async def demo_analysis(payload: DemoAnalysisRequest, request: Request):
        engine = runtime()
        if not isinstance(engine.providers.fixtures, FakeProvider):
            raise HTTPException(403, "Synthetic scenarios are available only with explicit --demo startup")
        check_rate_limit(request)
        now = datetime.now(timezone.utc)
        # Each explicit demo action creates a fresh isolated source bundle. Never replace
        # shared real providers or alter the current pipeline's settings/decision service.
        demonstration = Orchestrator(
            make_demo_bundle(payload.scenario, as_of=now),
            engine.settings,
            engine.repository,
            shadow=engine.shadow,
            system_two=engine.system_two,
            metrics=engine.metrics,
        )
        return await demonstration.analyze(
            AnalysisRequest(match_id="demo-match", as_of=now, markets=payload.markets, depth=payload.depth)
        )
