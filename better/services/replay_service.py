"""Strict offline execution of an archived, hash-verified Better input manifest."""

import copy
import hashlib
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from better import __version__
from better.config import Settings
from better.domain.enums import ErrorCode
from better.domain.errors import BetterError, ErrorDetail
from better.domain.models import (
    HistoricalPrediction,
    LineupSnapshot,
    Match,
    NewsSnapshot,
    OddsSnapshot,
    Portfolio,
    StatsSnapshot,
)
from better.domain.research import EnrichedSnapshot
from better.domain.schemas import AnalysisRequest, AnalysisRun
from better.orchestration.orchestrator import Orchestrator
from better.orchestration.snapshot import SnapshotProviders
from better.providers.base import ProviderBundle
from better.providers.llm import ReasoningRequest, ReasoningResponse
from better.storage.repository import Repository, canonical_hash
from better.system_one import ShadowCoordinator, SystemOneSettings
from better.system_zero.freshness import utc


class ReplayError(ValueError):
    """The archive cannot be reproduced without changing its recorded assumptions."""


class ArchivedSystemTwoProvider:
    """Reuses archived outputs; has no transport and cannot contact a model endpoint."""

    def __init__(self, model_id: str | None, records: tuple[dict, ...]):
        if model_id is not None:
            self.model_id = model_id
        self._archive = copy.deepcopy(records)
        self._cursor = 0
        self.records: list[dict] = []
        self.calls: list[ReasoningRequest] = []

    async def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        self.calls.append(request)
        if self._cursor >= len(self._archive):
            raise ReplayError(
                "System 2 output missing from archive; live inference is forbidden during replay"
            )
        while self._cursor < len(self._archive):
            record = self._archive[self._cursor]
            for field, expected in (
                ("model", request.model),
                ("temperature", request.temperature),
                ("prompt_version", request.prompt_version),
                ("input_hash", request.input_hash),
            ):
                if record.get(field) != expected:
                    raise ReplayError(f"System 2 archived request mismatch: {field}")
            self._cursor += 1
            self.records.append(copy.deepcopy(record))
            try:
                output = record["output"]
                response = (
                    ReasoningResponse.model_validate_json(output)
                    if isinstance(output, str)
                    else ReasoningResponse.model_validate(output)
                )
            except (ValidationError, KeyError, TypeError) as error:
                if record.get("valid") is not False:
                    raise ReplayError("System 2 archive falsely labels malformed output as valid") from error
                # A recorded repair attempt may be followed by a valid response for this request.
                if (
                    self._cursor < len(self._archive)
                    and self._archive[self._cursor].get("input_hash") == request.input_hash
                ):
                    continue
                raise BetterError(
                    ErrorCode.SCHEMA_ERROR, "System 2 output failed bounded schema repair"
                ) from error
            if record.get("valid") is not True:
                raise ReplayError("System 2 archive validation flag is inconsistent")
            return response
        raise ReplayError("System 2 archive exhausted")

    def assert_consumed(self) -> None:
        if self._cursor != len(self._archive):
            raise ReplayError("System 2 archive contains outputs that the replay never requested")


def _prompt_hashes() -> dict[str, str]:
    directory = Path(__file__).parents[1] / "prompts"
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.glob("*.md"))
    }


def _validate_temporal_inputs(match, as_of, snapshots, history, portfolio):
    if as_of >= match.kickoff:
        raise ReplayError("Archived as_of must precede kickoff")
    for name, snapshot in snapshots.items():
        if snapshot.match_id != match.match_id:
            raise ReplayError(f"Archived {name} snapshot belongs to another match")
        if name == "odds":
            if any(
                quote.match_id != match.match_id
                or quote.captured_at > as_of
                or not quote.provenance.available_at(as_of)
                for quote in snapshot.quotes
            ):
                raise ReplayError("Archived odds contain information unavailable at as_of")
        else:
            if snapshot.captured_at > as_of or any(
                not source.available_at(as_of) for source in snapshot.provenance
            ):
                raise ReplayError(f"Archived {name} unavailable at as_of")
            if name == "stats" and snapshot.data_cutoff > as_of:
                raise ReplayError("Archived statistical features exceed as_of")
            if name == "news" and any(not item.provenance.available_at(as_of) for item in snapshot.items):
                raise ReplayError("Archived news exceed as_of")
    if any(prediction.settled_at > as_of for prediction in history):
        raise ReplayError("Archived validation history contains outcomes after as_of")
    if portfolio and any(position.placed_at > as_of for position in portfolio.positions):
        raise ReplayError("Archived portfolio contains positions after as_of")


class ReplayService:
    """Re-run every actual agent from recorded inputs in a separate in-memory journal.

    System One is always disabled. System Two, when present, consumes only archived
    structured responses. A mismatch raises instead of silently declaring success.
    """

    async def replay(
        self, original: AnalysisRun, *, parent_runs: tuple[AnalysisRun, ...] = ()
    ) -> AnalysisRun:
        manifest = copy.deepcopy(original.input_manifest)
        if not manifest or canonical_hash(manifest) != original.input_hash:
            raise ReplayError("Input manifest is missing or its hash does not match the archived run")
        if original.snapshot.input_hash != original.input_hash:
            raise ReplayError("Snapshot and run input hashes differ")
        if original.analysis_id != uuid5(
            NAMESPACE_URL, f"better:{original.input_hash}"
        ) or original.run_id != uuid5(NAMESPACE_URL, f"better:run:{original.input_hash}"):
            raise ReplayError("Archived identifiers do not match the content-addressed input hash")
        if original.software_version != __version__ or original.schema_version != "1.0":
            raise ReplayError("Replay requires the archived software and schema versions")
        if manifest.get("software_version") != original.software_version:
            raise ReplayError("Input manifest software version differs from the archived run")
        if manifest.get("prompt_hashes") != _prompt_hashes():
            raise ReplayError("Current prompt hashes differ from the archived prompt files")
        if manifest.get("model_versions") != ["poisson:1.0.0"] or manifest.get("prompt_version") != "1.0.0":
            raise ReplayError("Archived model or prompt version is unsupported by this replay runtime")
        try:
            settings = Settings.model_validate(manifest["resolved_config"])
            match = Match.model_validate(manifest["match"])
            as_of = utc(datetime.fromisoformat(manifest["as_of"]))
            request = AnalysisRequest.model_validate(manifest["request"])
            history = tuple(HistoricalPrediction.model_validate(record) for record in manifest["history"])
            portfolio = (
                Portfolio.model_validate(manifest["portfolio"]) if manifest["portfolio"] is not None else None
            )
            classes = {
                "stats": StatsSnapshot,
                "odds": OddsSnapshot,
                "news": NewsSnapshot,
                "lineups": LineupSnapshot,
            }
            if set(manifest["snapshots"]) | set(manifest["failures"]) != set(classes):
                raise ReplayError("Each archived source must contain either a snapshot or a failure")
            if set(manifest["snapshots"]) & set(manifest["failures"]):
                raise ReplayError("An archived source cannot have both snapshot and failure")
            snapshots = {
                name: classes[name].model_validate(raw) for name, raw in manifest["snapshots"].items()
            }
            enriched = (
                EnrichedSnapshot.model_validate(manifest["enriched_snapshot"])
                if manifest.get("enriched_snapshot") is not None
                else None
            )
            failures = {}
            for name, raw in manifest["failures"].items():
                detail = ErrorDetail.model_validate(raw)
                error = BetterError(detail.code, detail.message, source=detail.source)
                error.detail = detail
                failures[name] = error
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            if isinstance(error, ReplayError):
                raise
            raise ReplayError(f"Invalid archived input contracts: {error}") from error
        if settings.version != manifest.get("config_version") or settings.version != original.config_version:
            raise ReplayError("Resolved configuration does not match its archived version")
        if settings.seed != manifest.get("seed") or settings.seed != original.seed:
            raise ReplayError("Archived simulation seeds disagree")
        if (
            as_of != original.as_of
            or match.match_id != original.match_id
            or request.match_id != match.match_id
        ):
            raise ReplayError("Archived request, match and as_of identifiers disagree")
        if request.as_of is not None and request.as_of != as_of:
            raise ReplayError("Explicit request as_of differs from analysis as_of")
        if (
            request.mode != original.mode
            or request.depth != original.depth
            or request.parent_analysis_id != original.parent_analysis_id
        ):
            raise ReplayError("Archived request execution options differ from the run")
        _validate_temporal_inputs(match, as_of, snapshots, history, portfolio)
        archived_snapshot = original.snapshot
        if (
            archived_snapshot.match != match
            or archived_snapshot.as_of != as_of
            or archived_snapshot.config_version != settings.version
        ):
            raise ReplayError("Snapshot metadata differs from input manifest")
        if any(getattr(archived_snapshot, name) != snapshots.get(name) for name in classes):
            raise ReplayError("Snapshot contents differ from input manifest")
        if archived_snapshot.enriched != enriched:
            raise ReplayError("Enriched snapshot differs from input manifest")
        if request.parent_analysis_id:
            parent = next((run for run in parent_runs if run.analysis_id == request.parent_analysis_id), None)
            if parent is None or parent.match_id != match.match_id or parent.as_of >= as_of:
                raise ReplayError("Replay of a child analysis requires its earlier parent run")
            if not parent.input_manifest or canonical_hash(parent.input_manifest) != parent.input_hash:
                raise ReplayError("Parent run input manifest failed integrity validation")
        records = tuple(copy.deepcopy(record) for record in original.system_two_records)
        model_id = manifest.get("system_two_model")
        if model_id is not None and not isinstance(model_id, str):
            raise ReplayError("Invalid archived System 2 model identifier")
        if not settings.system_two_enabled and records:
            raise ReplayError("System 2 outputs exist in a run where System 2 was disabled")
        system_two = (
            ArchivedSystemTwoProvider(model_id, records)
            if settings.system_two_enabled and (model_id is not None or records)
            else None
        )
        source = SnapshotProviders(match, snapshots, failures)
        providers = ProviderBundle(source, source, source, source, source, history, portfolio, system_two)
        repository = Repository("sqlite:///:memory:")
        try:
            orchestrator = Orchestrator(
                providers,
                settings,
                repository,
                shadow=ShadowCoordinator(SystemOneSettings(enabled=False)),
                system_two=system_two,
            )
            # The exact original request may have as_of=None. Calling analyze() would
            # introduce today's clock or alter the hashed request. All input validation
            # above replaces collection; _execute reruns the complete real agent workflow.
            replayed = await orchestrator._execute(
                request,
                as_of,
                match,
                providers,
                snapshots,
                history,
                original.input_hash,
                original.analysis_id,
                manifest,
                enriched,
            )
            if system_two is not None:
                system_two.assert_consumed()
            if replayed.input_hash != original.input_hash or replayed.analysis_id != original.analysis_id:
                raise ReplayError("Replay altered content-addressed identity")
            if replayed.decision != original.decision:
                raise ReplayError("Replay decision differs from the archived decision")
            if replayed.results != original.results or replayed.actual_agents != original.actual_agents:
                raise ReplayError(
                    "Replay agent results differ; unavailable external failure/response or software drift"
                )
            return replayed
        finally:
            repository.close()


async def replay_analysis(original: AnalysisRun, *, parent_runs: tuple[AnalysisRun, ...] = ()) -> AnalysisRun:
    return await ReplayService().replay(original, parent_runs=parent_runs)
