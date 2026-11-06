import asyncio
import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from time import monotonic
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from better import __version__
from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.domain.schemas import AnalysisRequest, AnalysisRun
from better.services.application import make_application
from better.services.report_service import human_report


def create_app(orchestrator=None, *, demo=None):
    owned = orchestrator is None

    @asynccontextmanager
    async def lifespan(app):
        from better.observability.logging import configure_logging

        if not logging.getLogger("better").handlers:
            configure_logging()
        if app.state.orchestrator is None:
            app.state.orchestrator = make_application(demo=demo)
        from better.services.data_pipeline import DataPipelineCollector, DataPipelineService

        pipeline = DataPipelineCollector(app.state.orchestrator)
        policy = DataPipelineService(app.state.orchestrator.repository)
        pipeline_task = asyncio.create_task(pipeline.run()) if policy.auto_collect else None
        app.state.data_pipeline_collector = pipeline
        yield
        if pipeline_task:
            pipeline_task.cancel()
            with suppress(asyncio.CancelledError):
                await pipeline_task
        if owned:
            await app.state.orchestrator.close()

    app = FastAPI(title="Bettor Football Decision Support", version=__version__, lifespan=lifespan)
    app.state.orchestrator = orchestrator
    requests = defaultdict(deque)

    def runtime():
        if app.state.orchestrator is None:
            raise HTTPException(503, "Application lifecycle not started")
        return app.state.orchestrator

    def get_run(identifier):
        run = runtime().repository.get_run(identifier)
        if run is None:
            raise HTTPException(404, "Analysis not found")
        return run

    def check_rate_limit(request):
        client = request.client.host if request.client else "unknown"
        now = monotonic()
        window = requests[client]
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= runtime().settings.analysis_requests_per_minute:
            raise HTTPException(429, "Analysis rate limit exceeded", headers={"Retry-After": "60"})
        window.append(now)

    @app.exception_handler(BetterError)
    async def handle_better_error(request, error):
        status = (
            404
            if error.detail.code == ErrorCode.MISSING_DATA
            else 422
            if error.detail.code == ErrorCode.CONFIGURATION_ERROR
            else 503
        )
        return JSONResponse(status_code=status, content={"error": error.detail.model_dump(mode="json")})

    @app.get("/health")
    async def health():
        engine = runtime()
        return {
            "status": "ok",
            "version": __version__,
            "execution_enabled": False,
            "system_one": engine.shadow.health(),
            "data_mode": "synthetic"
            if type(engine.providers.fixtures).__name__ == "FakeProvider"
            else "configured_or_unavailable",
        }

    @app.get("/matches")
    async def matches():
        return await runtime().providers.fixtures.list_matches(datetime.now(timezone.utc))

    @app.get("/matches/{match_id}")
    async def match(match_id: str):
        return await runtime().providers.fixtures.get_match(match_id)

    @app.post("/analysis", response_model=AnalysisRun)
    async def analyze(payload: AnalysisRequest, request: Request):
        check_rate_limit(request)
        return await runtime().analyze(payload)

    @app.get("/analysis/{analysis_id}", response_model=AnalysisRun)
    async def analysis(analysis_id: UUID):
        return get_run(analysis_id)

    @app.get("/analysis/{analysis_id}/agents")
    async def agents(analysis_id: UUID):
        return get_run(analysis_id).results

    @app.get("/analysis/{analysis_id}/decision")
    async def decision(analysis_id: UUID):
        return get_run(analysis_id).decision

    @app.get("/analysis/{analysis_id}/report", response_class=PlainTextResponse)
    async def report(analysis_id: UUID):
        return human_report(get_run(analysis_id))

    @app.get("/analysis/{analysis_id}/shadow")
    async def shadow(analysis_id: UUID):
        get_run(analysis_id)
        return runtime().repository.list_records("shadow_decisions", entity_id=analysis_id)

    @app.get('/models/diagnostics')
    async def model_diagnostics():
        from better.services.model_diagnostics import ModelDiagnostics
        return ModelDiagnostics(runtime().repository).report()

    @app.get("/models")
    async def models():
        return [
            {
                "model": "poisson",
                "version": "1.0.0",
                "operational": True,
                "requires_validation_history": True,
            },
            *(
                {
                    "model": model,
                    "operational": False,
                    "status": "quantitative_baseline_not_admitted_without_validation",
                }
                for model in ("dixon_coles", "elo", "monte_carlo", "logistic", "ensemble")
            ),
        ]

    @app.get("/models/{model_id}/validation")
    async def validation(model_id: str):
        return runtime().repository.list_records("validation_metrics", entity_id=model_id)

    @app.get("/journal")
    async def journal(last: int = Query(default=20, ge=1, le=1000)):
        return runtime().repository.list_records("journal_entries", last=last)

    @app.get("/metrics")
    async def metrics():
        return {"pipeline": runtime().metrics.snapshot(), "system_one": runtime().shadow.telemetry()}

    from better.api.dashboard import mount_dashboard
    from better.api.salabet import mount_salabet

    mount_dashboard(app, runtime, check_rate_limit)
    mount_salabet(app, runtime, check_rate_limit)
    from better.api.settings import mount_settings
    mount_settings(app, runtime)
    return app


app = create_app()
