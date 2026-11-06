import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from better import __version__
from better.config import Settings
from better.domain.enums import AgentStatus, AnalysisDepth, ErrorCode
from better.domain.errors import BetterError, ErrorDetail
from better.domain.models import Degradation
from better.domain.schemas import (
    AgentContext,
    AgentResult,
    AnalysisRequest,
    AnalysisRun,
    DecisionReport,
    InputSnapshot,
)
from better.observability.metrics import Metrics
from better.orchestration.event_bus import EventBus
from better.orchestration.snapshot import SnapshotCollector
from better.orchestration.workflow import SPORTS_LAYERS
from better.services.decision_service import DecisionService
from better.services.forecast_service import ForecastService
from better.storage.repository import Repository, canonical_hash
from better.system_one import ShadowCoordinator, SystemOneSettings, SystemOneStateBuilder

logger = logging.getLogger("better.orchestrator")


class Orchestrator:
    def __init__(
        self,
        providers,
        settings: Settings | None = None,
        repository: Repository | None = None,
        *,
        shadow: ShadowCoordinator | None = None,
        system_two=None,
        metrics=None,
        event_bus=None,
        external_research=None,
        football_data_research=None,
    ):
        self.settings = settings or Settings.load()
        self.providers = providers
        self.repository = repository or Repository(self.settings.database_url)
        self.shadow = shadow or ShadowCoordinator(SystemOneSettings.from_env())
        self.system_two = system_two if system_two is not None else providers.system_two
        self.metrics = metrics or Metrics()
        self.events = event_bus or EventBus()
        self.external_research = external_research
        self.football_data_research = football_data_research
        self.collector = SnapshotCollector(providers, self.settings)
        self._locks = {}

    async def analyze(self, request: AnalysisRequest, *, cache_only_research=False, research_budget=None) -> AnalysisRun:
        as_of = request.as_of or datetime.now(timezone.utc)
        fixture_provider = self.providers.fixtures
        match = (
            await fixture_provider.get_match(request.match_id)
            if hasattr(fixture_provider, "executor") or getattr(fixture_provider, "manages_execution", False)
            else await self.collector.executors["fixtures"].call(
                lambda: fixture_provider.get_match(request.match_id)
            )
        )
        if match.match_id != request.match_id:
            raise BetterError(ErrorCode.SCHEMA_ERROR, "Fixture identity mismatch")
        if self.settings.active_competitions and match.competition not in self.settings.active_competitions:
            raise BetterError(ErrorCode.CONFIGURATION_ERROR, "COMPETITION_DISABLED: competizione esclusa dal workspace operativo")
        if as_of >= match.kickoff:
            raise BetterError(ErrorCode.CONFIGURATION_ERROR, "V1 supports analysis strictly before kickoff")
        if request.parent_analysis_id:
            parent = self.repository.get_run(request.parent_analysis_id)
            if parent is None or parent.match_id != match.match_id or parent.as_of >= as_of:
                raise BetterError(
                    ErrorCode.CONFIGURATION_ERROR, "Parent must be an earlier analysis of this match"
                )
        providers, collected, failures = await self.collector.collect(
            match, as_of, live=request.as_of is None
        )
        if request.as_of is None:
            as_of = datetime.now(timezone.utc)
        research_match = match
        if hasattr(fixture_provider, "competition_names"):
            competition_name = fixture_provider.competition_names().get(match.competition)
            if competition_name:
                research_match = match.model_copy(update={"competition": competition_name})
        enriched = None
        research_result = None
        football_data_result = None
        football_data = self.football_data_research
        from better.services.football_data_research import merge_football_stats, missing_model_research_fields

        missing_stats = missing_model_research_fields(collected.get("stats"), self.settings.min_training_sample)
        if (request.as_of is None and football_data is not None and football_data.settings.enabled
                and missing_stats):
            from better.services.football_data_research import normalize_football_data

            research_options = {"cache_only": True} if cache_only_research else {}
            if research_budget is not None:
                football_data_result = await research_budget.execute("football_data", football_data.settings.max_cost_per_match,
                    lambda **options: football_data.research(research_match, as_of, requested_fields=missing_stats, **options))
            else:
                football_data_result = await football_data.research(research_match, as_of, requested_fields=missing_stats, **research_options)
            stats = merge_football_stats(collected.get("stats"), normalize_football_data(match, football_data_result)) if football_data_result else collected.get("stats")
            if stats is not None:
                collected["stats"] = stats
                failures.pop("stats", None)
            as_of = datetime.now(timezone.utc)
        research = self.external_research
        if (
            request.as_of is None
            and research is not None
            and research.settings.enabled
        ):
            from better.services.external_research import missing_qualitative_fields
            fields = tuple(name for name, policy in research.settings.fields.items() if policy.enabled) if research.settings.research_primary_source else missing_qualitative_fields(collected, as_of, research.settings, news_ttl=self.settings.news_ttl_seconds, kickoff=match.kickoff)
            # Qualitative facts (availability, lineups, press conferences, weather)
            # do not change when an 888 price ticks.  Including odds here caused a
            # refreshed quote snapshot to bypass the research cache and immediately
            # exhaust the one-request-per-window budget.
            from better.services.external_research import qualitative_source_state

            source_state = qualitative_source_state(match)
            if research_budget is not None:
                research_result = await research_budget.execute("external", research.settings.max_cost_per_match,
                    lambda **options: research.research_missing(research_match, as_of=as_of,
                        missing_fields=fields, source_state_version=source_state, **options))
            else:
                research_result = await research.research_missing(
                    research_match, as_of=as_of, missing_fields=fields, source_state_version=source_state,
                    **({"cache_only": True} if cache_only_research else {}))
            as_of = datetime.now(timezone.utc)
        if research_result is not None or football_data_result is not None:
            from better.services.research_enrichment import (
                build_enriched_snapshot,
                normalized_qualitative_snapshots,
            )

            enriched = build_enriched_snapshot(
                match,
                research_result,
                football_result=football_data_result,
                odds=collected.get("odds"),
            )
            if research_result is not None:
                qualitative = normalized_qualitative_snapshots(match, enriched)
                for name, snapshot in qualitative.items():
                    collected[name] = snapshot
                    failures.pop(name, None)
                if "news" not in qualitative and collected.get("news") is None:
                    from better.domain.research import ResearchRunStatus

                    error = research_result.error or "Nessuna evidenza news utilizzabile"
                    if "OPENAI_KEY_EXPIRED" in error:
                        failures["news"] = BetterError(
                            ErrorCode.PERMANENT_PROVIDER_ERROR,
                            "OpenAI non disponibile: autenticazione fallita (OPENAI_KEY_EXPIRED): chiave scaduta",
                            source="openai_research",
                        )
                    elif "OPENAI_PERMISSION_ERROR" in error:
                        failures["news"] = BetterError(
                            ErrorCode.PERMANENT_PROVIDER_ERROR,
                            "OpenAI non disponibile: permessi insufficienti (OPENAI_PERMISSION_ERROR)",
                            source="openai_research",
                        )
                    elif "OPENAI_API_KEY" in error:
                        failures["news"] = BetterError(
                            ErrorCode.PERMANENT_PROVIDER_ERROR,
                            "OpenAI non disponibile: autenticazione fallita",
                            source="openai_research",
                        )
                    elif research_result.status == ResearchRunStatus.RESEARCH_RATE_LIMIT:
                        failures["news"] = BetterError(
                            ErrorCode.RATE_LIMIT,
                            "OpenAI Research: limite richieste raggiunto",
                            source="openai_research",
                        )
                    elif research_result.status == ResearchRunStatus.RESEARCH_TIMEOUT:
                        failures["news"] = BetterError(
                            ErrorCode.TIMEOUT,
                            "OpenAI Research: tempo di risposta superato",
                            source="openai_research",
                        )
                    elif research_result.status in {
                        ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                        ResearchRunStatus.EXTERNAL_RESEARCH_ERROR,
                        ResearchRunStatus.RESEARCH_SCHEMA_ERROR,
                    }:
                        failures["news"] = BetterError(
                            ErrorCode.PERMANENT_PROVIDER_ERROR,
                            f"OpenAI Research non disponibile: {error}",
                            source="openai_research",
                        )
                    else:
                        failures["news"] = BetterError(
                            ErrorCode.MISSING_DATA,
                            "OpenAI Research completata senza evidenze news utilizzabili",
                            source="openai_research",
                        )
            providers = self.collector.freeze(match, collected, failures)
        if providers.portfolio is None:
            from better.services.bank_service import BankService

            providers = replace(providers, portfolio=BankService(self.repository).portfolio(as_of))
        if as_of >= match.kickoff:
            raise BetterError(ErrorCode.CONFIGURATION_ERROR, "Source collection completed after kickoff")
        fixture_record = (
            fixture_provider.latest_record(request.match_id)
            if hasattr(fixture_provider, "latest_record")
            else None
        )
        if fixture_record and not fixture_record.provenance.available_at(as_of):
            raise BetterError(
                ErrorCode.MISSING_DATA,
                "Live fixture metadata cannot be used in an earlier historical analysis",
            )
        persisted_history = self.repository.settled_validation_history(as_of)
        historical = tuple(p for p in providers.validation_history if p.settled_at <= as_of)
        historical = tuple(
            {
                (
                    p.model_id,
                    p.model_version,
                    p.competition,
                    p.market,
                    p.selection,
                    p.match_id,
                    p.predicted_at,
                ): p
                for p in (*historical, *persisted_history)
            }.values()
        )
        material = {
            "software_version": __version__,
            "match": match.model_dump(mode="json"),
            "as_of": as_of.isoformat(),
            "snapshots": {name: value.model_dump(mode="json") for name, value in sorted(collected.items())},
            "enriched_snapshot": enriched.model_dump(mode="json") if enriched else None,
            "research_result": research_result.model_dump(mode="json") if research_result else None,
            "football_data_result": football_data_result.model_dump(mode="json")
            if football_data_result
            else None,
            "failures": {
                name: error.detail.model_dump(mode="json") for name, error in sorted(failures.items())
            },
            "history": [p.model_dump(mode="json") for p in historical],
            "portfolio": providers.portfolio.model_dump(mode="json") if providers.portfolio else None,
            "request": request.model_dump(mode="json"),
            "config_version": self.settings.version,
            "model_versions": ["poisson:1.0.0"],
            "prompt_version": "1.0.0",
            "seed": self.settings.seed,
            "resolved_config": self.settings.decision_configuration(),
            "prompt_hashes": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((Path(__file__).parents[1] / "prompts").glob("*.md"))
            },
            "system_two_model": getattr(self.system_two, "model_id", None)
            if self.settings.system_two_enabled
            else None,
        }
        if cache_only_research:
            material["cache_only_research"] = True
        if research_budget is not None:
            material["research_budget_guarded"] = True
            material["automation_research_usage"] = research_budget.summary()
        if fixture_record:
            material["fixture_record"] = fixture_record.model_dump(mode="json")
        # Only a genuinely live 888 acquisition can start a new historical row.
        # Explicit historical/demo/replay cutoffs are never re-labelled as if the
        # evidence had been captured at that earlier time.
        if request.as_of is None and fixture_record is not None:
            from better.services.data_pipeline import checkpoint_for

            self.repository.select_for_collection(match=match, selected_at=as_of)
            lineup = collected.get("lineups")
            checkpoint = checkpoint_for(
                match.kickoff,
                as_of,
                official_lineup=bool(lineup and lineup.status == "CONFIRMED_LINEUP"),
            )
            if not self.repository.has_historical_checkpoint(match.match_id, checkpoint):
                self.repository.archive_pre_match_observation(
                    match=match,
                    as_of=as_of,
                    collected_at=as_of,
                    snapshots=collected,
                    failures=failures,
                    purpose="ANALYSIS_INPUT",
                    checkpoint=checkpoint,
                )
        input_hash = canonical_hash(material)
        analysis_id = uuid5(NAMESPACE_URL, f"better:{input_hash}")
        lock = self._locks.setdefault(str(analysis_id), asyncio.Lock())
        async with lock:
            existing = self.repository.get_run(analysis_id)
            if existing:
                return existing
            return await self._execute(
                request,
                as_of,
                match,
                providers,
                collected,
                historical,
                input_hash,
                analysis_id,
                material,
                enriched,
            )

    async def _execute(
        self,
        request,
        as_of,
        match,
        providers,
        collected,
        historical,
        input_hash,
        analysis_id,
        material,
        enriched,
    ):
        from better.agents.registry import create_registry

        registry = create_registry(providers, self.settings, journal=self.repository)
        # System 2 is injected explicitly and never selected/disabled by System One.
        if self.system_two is not None and "tactical_analyst" in registry.ids():
            registry.get("tactical_analyst").system_two = self.system_two
        snapshot = InputSnapshot(
            match=match,
            as_of=as_of,
            **collected,
            enriched=enriched,
            config_version=self.settings.version,
            model_versions=("poisson:1.0.0",),
            input_hash=input_hash,
        )
        run_id = uuid5(NAMESPACE_URL, f"better:run:{input_hash}")
        results = {}
        actual_agents = []
        observer_tasks = []
        observer_records = []
        system_two_record_start = len(getattr(self.system_two, "records", ())) if self.system_two else 0
        system_two_call_start = len(getattr(self.system_two, "calls", ())) if self.system_two else 0

        def observe(stage, outputs=None):
            # Crucially: no state builder and no provider object is touched while disabled.
            if not self.shadow.settings.enabled:
                return
            try:
                state = SystemOneStateBuilder().build(snapshot.model_dump(mode="json"), outputs)
            except Exception:
                logger.warning("shadow_state_unavailable", extra={"analysis_id": str(analysis_id)})
                return
            observer_tasks.append((stage, state))

        observe("pre_screen")
        observe("routing")

        def scoped_context(agent):
            dependencies = agent.dependencies
            return AgentContext(
                analysis_id=analysis_id,
                run_id=run_id,
                match=match,
                as_of=as_of,
                requested_markets=request.markets,
                mode=request.mode,
                depth=request.depth,
                seed=self.settings.seed,
                upstream_results={
                    name: results[name].model_copy(deep=True) for name in dependencies if name in results
                },
                enriched_snapshot=enriched.model_copy(deep=True) if enriched else None,
                validation_history=historical if agent.needs_history else (),
                portfolio=providers.portfolio.model_copy(deep=True)
                if agent.needs_portfolio and providers.portfolio
                else None,
            )

        async def execute(agent_id):
            started = perf_counter()
            agent = registry.get(agent_id)
            enabled = registry.is_enabled(agent_id)
            if request.depth == AnalysisDepth.FAST and agent_id == "tactical_analyst":
                enabled = False
            if not enabled:
                result = AgentResult(
                    analysis_id=analysis_id,
                    agent_id=agent_id,
                    status=AgentStatus.INSUFFICIENT_DATA,
                    summary="Agent disabled by explicit configuration",
                    generated_at=as_of,
                    unknowns=("DISABLED",),
                )
            else:
                actual_agents.append(agent_id)
                try:
                    result = await agent.run(scoped_context(agent))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "agent_unhandled_failure", extra={"analysis_id": str(analysis_id), "agent": agent_id}
                    )
                    result = AgentResult(
                        analysis_id=analysis_id,
                        agent_id=agent_id,
                        status=AgentStatus.MODEL_ERROR,
                        summary="Agent failed without producing a usable result",
                        generated_at=as_of,
                        errors=(
                            ErrorDetail(
                                code=ErrorCode.MODEL_FAILURE,
                                message="Unhandled agent failure",
                                source=agent_id,
                            ),
                        ),
                    )
            results[agent_id] = result
            duration_ms = (perf_counter() - started) * 1000
            self.metrics.agent_completed(agent_id, result.status.value, duration_ms)
            logger.info(
                "orchestrator_agent_result_received",
                extra={
                    "analysis_id": str(analysis_id),
                    "run_id": str(run_id),
                    "agent": agent_id,
                    "status": result.status.value,
                    "duration_ms": duration_ms,
                    "degradations": [d.model_dump(mode="json") for d in result.degradations],
                },
            )
            self.events.emit(
                {
                    "event": "agent_completed",
                    "analysis_id": str(analysis_id),
                    "run_id": str(run_id),
                    "agent": agent_id,
                    "status": result.status.value,
                    "duration_ms": duration_ms,
                }
            )

        for layer in SPORTS_LAYERS:
            if "professional_bettor" in layer:
                output_snapshot = {
                    name: result.model_dump(mode="json") for name, result in sorted(results.items())
                }
                observe("consistency", output_snapshot)
                observe("escalation", output_snapshot)
            await asyncio.gather(*(execute(agent_id) for agent_id in layer))
            # A disabled/failed Chief Analyst cannot stop a conservative terminal status.
            if "professional_bettor" in layer and not isinstance(
                results["professional_bettor"].payload, DecisionReport
            ):
                context = AgentContext(
                    analysis_id=analysis_id,
                    run_id=run_id,
                    match=match,
                    as_of=as_of,
                    requested_markets=request.markets,
                    upstream_results={k: v for k, v in results.items() if k != "professional_bettor"},
                    enriched_snapshot=enriched.model_copy(deep=True) if enriched else None,
                    portfolio=providers.portfolio,
                    mode=request.mode,
                    depth=request.depth,
                    seed=self.settings.seed,
                )
                decision = DecisionService(self.settings).evaluate(context)
                # Explicit Chief failure must never be silently turned into BET by recovery.
                from better.domain.enums import DecisionStatus
                from better.domain.models import GateTrace, Stake

                decision = decision.model_copy(
                    update={
                        "status": DecisionStatus.INSUFFICIENT_DATA,
                        "stake": Stake(),
                        "decision_trace": decision.decision_trace
                        + (
                            GateTrace(
                                gate="CHIEF_AVAILABILITY", status="FAIL", reason="Chief agent unavailable"
                            ),
                        ),
                    }
                )
                results["professional_bettor"] = AgentResult(
                    analysis_id=analysis_id,
                    agent_id="professional_bettor",
                    status=AgentStatus.PARTIAL,
                    summary="Deterministic conservative recovery",
                    degradations=(
                        Degradation(
                            code="CHIEF_AGENT_UNAVAILABLE",
                            missing_checks=("chief_agent_result",),
                            critical=True,
                            affected_gates=("CONTEXT_INTEGRITY",),
                            impact="BLOCK",
                            reason="The chief agent did not produce a decision. Recovery only emits "
                            "INSUFFICIENT_DATA with zero stake.",
                        ),
                    ),
                    generated_at=as_of,
                    payload=DecisionReport(decision=decision),
                )

        decision = results["professional_bettor"].payload.decision
        forecast = ForecastService().build(results, as_of, request.markets)
        actual = {
            "agents": sorted(actual_agents),
            "decision": decision.status.value,
            "system2_calls": len(getattr(self.system_two, "calls", ())) - system_two_call_start
            if self.system_two
            else 0,
        }
        # Evaluate copied pre/post states after the real decision. Suggestions have no writable
        # reference to pipeline state; latency/errors cannot change freshness or agent scheduling.
        if observer_tasks:
            observer_records = [
                r
                for r in await asyncio.gather(
                    *(
                        self.shadow.observe(str(analysis_id), stage, state, actual)
                        for stage, state in observer_tasks
                    )
                )
                if r is not None
            ]
        run = AnalysisRun(
            analysis_id=analysis_id,
            run_id=run_id,
            match_id=match.match_id,
            created_at=as_of,
            as_of=as_of,
            config_version=self.settings.version,
            seed=self.settings.seed,
            mode=request.mode,
            depth=request.depth,
            parent_analysis_id=request.parent_analysis_id,
            results=results,
            decision=decision,
            forecast=forecast,
            snapshot=snapshot,
            actual_agents=tuple(sorted(actual_agents)),
            input_hash=input_hash,
            input_manifest=material,
            system_two_records=tuple(getattr(self.system_two, "records", ())[system_two_record_start:])
            if self.system_two
            else (),
        )
        self.repository.save_run(run, shadow_records=observer_records)
        self.metrics.decisions[decision.status.value] += 1
        return run

    async def close(self):
        if self.football_data_research is not None:
            await self.football_data_research.close()
        if self.external_research is not None:
            await self.external_research.close()
        closed = set()
        for source in (
            self.providers.fixtures,
            self.providers.stats,
            self.providers.odds,
            self.providers.news,
            self.providers.lineups,
        ):
            if id(source) not in closed and hasattr(source, "close"):
                await source.close()
                closed.add(id(source))
        result_provider = self.providers.results
        if (
            result_provider is not None
            and id(result_provider) not in closed
            and hasattr(result_provider, "close")
        ):
            await result_provider.close()
        self.repository.close()
