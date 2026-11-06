"""SalaBet HTTP workspace over authoritative Better analyses."""

from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse

from better.domain.salabet import (
    BetCandidateCreate,
    BetSelectionVoid,
    BetSlipAction,
    BetSlipCreate,
    BetSlipUpdate,
)
from better.services.automatic_multiples import (
    AutomaticMultiples,
    MultipleAutoSwitch,
    MultipleBudget,
    MultipleSwitch,
)
from better.services.salabet_service import SalaBetService

WEB_ROOT = Path(__file__).parents[1] / "web"


def mount_salabet(app, runtime, check_rate_limit):
    def service() -> SalaBetService:
        return SalaBetService(runtime().repository, settings=runtime().settings)

    def mutation(request: Request):
        check_rate_limit(request)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origine della richiesta non consentita")

    def call(operation):
        try:
            return operation()
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/salabet", include_in_schema=False)
    async def salabet_workspace():
        return FileResponse(
            WEB_ROOT / "salabet.html",
            headers={
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            },
        )

    @app.post("/salabet/candidates")
    async def create_candidate(payload: BetCandidateCreate, request: Request):
        mutation(request)
        return call(lambda: service().create_candidate(payload))

    @app.post("/salabet/candidates/indicative")
    async def create_indicative_candidate(payload: BetCandidateCreate, request: Request):
        mutation(request)
        return call(lambda: service().create_indicative_candidate(payload))

    @app.get("/salabet/candidates")
    async def candidates(include_removed: bool = False):
        from better.services.timing_comparison import TimingComparisonService
        timing = TimingComparisonService(runtime())
        rows = []
        for candidate in service().list_candidates(include_removed=include_removed):
            row = candidate.model_dump(mode="json")
            row["timing_comparison"] = timing.view(row["match_id"], row["kickoff"], candidate=row) if row["competition_id"] in runtime().settings.timing_comparison_competitions else None
            rows.append(row)
        return rows

    @app.get("/salabet/automatic-multiples")
    async def automatic_multiples():
        return AutomaticMultiples(runtime()).report()

    @app.post("/salabet/automatic-multiples")
    async def update_automatic_multiples(payload: MultipleSwitch, request: Request):
        mutation(request)
        return call(lambda: AutomaticMultiples(runtime()).update(payload))

    @app.post('/salabet/automatic-multiples/budget')
    async def multiple_budget(payload: MultipleBudget, request: Request):
        mutation(request)
        return call(lambda: AutomaticMultiples(runtime()).update_policy(payload))

    @app.post('/salabet/automatic-multiples/placement')
    async def multiple_placement(payload: MultipleAutoSwitch, request: Request):
        mutation(request)
        worker = AutomaticMultiples(runtime())
        call(lambda: worker.update_policy(payload))
        result = await worker.collect_verified() if payload.enabled else {'enabled': False, 'created': [], 'diagnostics': []}
        return {**worker.report(), 'execution': result}

    @app.post("/salabet/candidates/{candidate_id}/remove")
    async def remove_candidate(candidate_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        return call(lambda: service().remove_candidate(candidate_id, payload))

    @app.post("/salabet/candidates/{candidate_id}/restore")
    async def restore_candidate(candidate_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        return call(lambda: service().restore_candidate(candidate_id, payload))

    @app.post("/salabet/slips")
    async def create_slip(payload: BetSlipCreate, request: Request):
        mutation(request)
        desk = service()
        return call(lambda: desk.slip_view(desk.create_slip(payload)))

    @app.get("/salabet/slips")
    async def slips():
        desk = service()
        return [desk.slip_view(item) for item in desk.list_visible_slips()]

    @app.get("/salabet/slips/{slip_id}")
    async def slip(slip_id: UUID):
        desk = service()
        return call(lambda: desk.slip_view(desk.get_slip(slip_id)))

    @app.patch("/salabet/slips/{slip_id}")
    async def update_slip(slip_id: UUID, payload: BetSlipUpdate, request: Request):
        mutation(request)
        desk = service()
        return call(lambda: desk.slip_view(desk.update_slip(slip_id, payload)))

    @app.post("/salabet/slips/{slip_id}/confirm")
    async def confirm_slip(slip_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        desk = service()
        return call(lambda: desk.slip_view(desk.confirm_slip(slip_id, payload)))

    @app.post("/salabet/slips/{slip_id}/place")
    async def place_slip(slip_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        desk = service()
        return call(lambda: desk.slip_view(desk.place_slip(slip_id, payload)))

    @app.post("/salabet/slips/{slip_id}/cancel")
    async def cancel_slip(slip_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        desk = service()
        return call(lambda: desk.slip_view(desk.cancel_slip(slip_id, payload)))

    @app.post("/salabet/slips/{slip_id}/selections/{candidate_id}/void")
    async def void_selection(
        slip_id: UUID,
        candidate_id: UUID,
        payload: BetSelectionVoid,
        request: Request,
    ):
        mutation(request)
        desk = service()
        return call(lambda: desk.slip_view(desk.void_selection(slip_id, candidate_id, payload)))

    @app.post("/salabet/slips/{slip_id}/hide")
    async def hide_slip(slip_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        return call(lambda: service().hide_slip(slip_id, payload))

    @app.post("/salabet/slips/{slip_id}/restore")
    async def restore_slip(slip_id: UUID, payload: BetSlipAction, request: Request):
        mutation(request)
        return call(lambda: service().restore_slip(slip_id, payload))

    @app.post("/salabet/settle")
    async def settle_salabet(request: Request):
        mutation(request)
        return call(lambda: service().settle_verified())

    @app.get("/salabet/archive")
    async def archive(
        date_from: date | None = None,
        date_to: date | None = None,
        bookmaker: str | None = None,
        market: str | None = None,
        competition: str | None = None,
        status: str | None = None,
        slip_type: str | None = Query(default=None, alias="type"),
        include_hidden: bool = False,
    ):
        rows = service().archive_records(include_hidden=include_hidden)
        if date_from:
            rows = [row for row in rows if date.fromisoformat(row["created_at"][:10]) >= date_from]
        if date_to:
            rows = [row for row in rows if date.fromisoformat(row["created_at"][:10]) <= date_to]
        if bookmaker:
            rows = [row for row in rows if row["bookmaker"] == bookmaker]
        if market:
            rows = [row for row in rows if any(item["market"] == market for item in row["selections"])]
        if competition:
            rows = [
                row
                for row in rows
                if any(item["competition_id"] == competition for item in row["selections"])
            ]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if slip_type:
            rows = [row for row in rows if row["type"] == slip_type]
        return rows

    @app.get("/salabet/stats")
    async def stats():
        return service().stats()
