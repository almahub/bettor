"""Operational view of the central data pipeline.

This service does not manufacture observations or promote raw history to model
validation.  It reports which immutable stages actually exist and keeps storage
policy separate from decision policy.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic_ns, time_ns
from uuid import uuid4

from sqlalchemy import func, select

from better.agents.registry import AGENT_REGISTRY
from better.config import Settings
from better.observability.diagnostics import failure_detail
from better.storage.db import TABLES
from better.system_zero.validation import validate_model

CHECKPOINTS = ("EARLY", "T24", "T6", "T1", "LINEUP", "CLOSING")


def checkpoint_for(kickoff: datetime, captured_at: datetime, *, official_lineup: bool = False) -> str:
    """Classify a pre-match capture without pretending it is a closing price."""
    remaining = kickoff - captured_at
    if remaining <= timedelta(0):
        raise ValueError("Checkpoint must precede kickoff")
    hours = remaining.total_seconds() / 3600
    if official_lineup and hours <= 1.5:
        return "LINEUP"
    if hours <= 0.25:
        return "CLOSING"
    if hours <= 1.5:
        return "T1"
    if hours <= 8:
        return "T6"
    if hours <= 30:
        return "T24"
    return "EARLY"


class DataPipelineService:
    """Read-only control plane for collection, history, validation and storage."""

    def __init__(self, repository, *, root: Path | None = None, now=None, settings=None):
        self.repository = repository
        self.root = (root or Path.cwd()).resolve()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.settings = settings or Settings()
        self.max_storage_gb = self._positive_float("BETTER_STORAGE_MAX_GB", 5.0)
        self.raw_retention_days = self._positive_int("BETTER_RAW_RETENTION_DAYS", 30)
        self.cache_retention_days = self._positive_int("BETTER_CACHE_RETENTION_DAYS", 7)
        self.auto_collect = os.environ.get("BETTER_PIPELINE_AUTO_COLLECT", "true").lower() == "true"
        self.poll_seconds = self._positive_int("BETTER_PIPELINE_POLL_SECONDS", 300)

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, default))
        except ValueError:
            return default
        return value if value > 0 else default

    @staticmethod
    def _positive_float(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, default))
        except ValueError:
            return default
        return value if value > 0 else default

    def report(self) -> dict:
        generated_at = self.now()
        counts = self._record_counts()
        observations = self.repository.all_records("historical_observations")
        analyses = self.repository.all_records("analysis_runs")
        results = self.repository.all_records("match_results")
        closing = self.repository.all_records("closing_prices")
        predictions = self.repository.all_records("settled_predictions")
        canonical_predictions = self.repository.all_records("canonical_predictions")
        validations = self.repository.all_records("validation_metrics")
        matches = self.repository.all_records("matches")
        selections = self.repository.active_collection_selections(generated_at)
        storage = self._storage_report(generated_at)
        from better.services.settlement_service import SettlementService
        from better.services.validation_cohort import ValidationCohortService

        pending_settlement = SettlementService(self.repository, clock=self.now).pending()
        return {
            "generated_at": generated_at.isoformat(),
            "timing_comparison": self._timing_report(),
            "automatic_multiples": self._multiples_report(),
            "status": "READY",
            "policy": {
                "selection": "SELECTED_MATCHES_ONLY",
                "validation_selection": "AUTO_ENROLLED_VERIFIED_COMPETITIONS",
                "checkpoints": list(CHECKPOINTS),
                "raw_retention_days": self.raw_retention_days,
                "cache_retention_days": self.cache_retention_days,
                "max_storage_gb": self.max_storage_gb,
                "database_records_are_immutable": True,
                "auto_collect": self.auto_collect,
                "poll_seconds": self.poll_seconds,
            },
            "selections": {"active": len(selections), "matches": selections},
            "records": counts,
            "checkpoints": self._checkpoint_summary(observations),
            "maturity": self._maturity(
                matches,
                observations,
                analyses,
                [*predictions, *canonical_predictions],
                results,
                closing,
            ),
            "agent_coverage": self._agent_coverage(analyses),
            "validation": self._validation_summary(validations, predictions),
            "validation_cohort": ValidationCohortService(
                repository=self.repository,
                settings=self.settings,
                clock=self.now,
            ).report(),
            "storage": storage,
            "settlement": {
                "pending": pending_settlement,
                "recorded_results": len(results),
                "settled_predictions": len(predictions),
                "closing_prices": len(closing),
            },
        }

    def _timing_report(self):
        from better.services.timing_comparison import TimingComparisonService
        return TimingComparisonService(repository=self.repository, settings=self.settings, clock=self.now).report()

    def _multiples_report(self):
        from types import SimpleNamespace

        from better.services.automatic_multiples import AutomaticMultiples
        return AutomaticMultiples(SimpleNamespace(repository=self.repository, settings=self.settings), clock=self.now).report()

    def _record_counts(self) -> dict[str, int]:
        with self.repository.engine.connect() as connection:
            return {
                name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                for name, table in TABLES.items()
            }

    @staticmethod
    def _checkpoint_summary(observations: list[dict]) -> dict:
        counts = Counter()
        for row in observations:
            checkpoint = row.get("checkpoint")
            if not checkpoint:
                try:
                    checkpoint = checkpoint_for(
                        datetime.fromisoformat(row["match"]["kickoff"]),
                        datetime.fromisoformat(row["collected_at"]),
                    )
                except (KeyError, TypeError, ValueError):
                    checkpoint = "UNKNOWN"
            counts[checkpoint] += 1
        return {"total": len(observations), "by_checkpoint": dict(sorted(counts.items()))}

    @staticmethod
    def _maturity(matches, observations, analyses, predictions, results, closing) -> dict:
        stages: dict[str, set[str]] = defaultdict(set)
        labels: dict[str, str] = {}
        for match in matches:
            identifier = match.get("match_id") or match.get("id")
            if identifier:
                stages[identifier].add("DISCOVERED")
                labels[identifier] = f"{match.get('home_team', '')} – {match.get('away_team', '')}".strip(
                    " –"
                )
        for row in observations:
            identifier = row.get("match", {}).get("match_id")
            if identifier:
                stages[identifier].update(("DISCOVERED", "SNAPSHOT_CAPTURED"))
        for run in analyses:
            identifier = run.get("match_id")
            if identifier:
                stages[identifier].update(("DISCOVERED", "ANALYZED"))
                match = run.get("snapshot", {}).get("match", {})
                labels[identifier] = f"{match.get('home_team', '')} – {match.get('away_team', '')}".strip(
                    " –"
                )
        for prediction in predictions:
            identifier = prediction.get("match_id")
            if identifier:
                stages[identifier].update(("PREDICTED", "SETTLED"))
        # Older prediction payloads do not carry match_id; analysis results remain
        # the authoritative link between a prediction and a fixture.
        for run in analyses:
            identifier = run.get("match_id")
            predictive = run.get("results", {}).get("predictive_model", {}).get("payload") or {}
            if identifier and predictive.get("model_predictions"):
                stages[identifier].add("PREDICTED")
        for result in results:
            identifier = result.get("match_id")
            if identifier:
                stages[identifier].add("SETTLED")
        for price in closing:
            identifier = price.get("match_id")
            if identifier:
                stages[identifier].add("CLOSING_PRICE_CAPTURED")
                continue
            analysis_id = price.get("analysis_id")
            run = next((item for item in analyses if str(item.get("analysis_id")) == str(analysis_id)), None)
            if run:
                stages[run["match_id"]].add("CLOSING_PRICE_CAPTURED")
        order = {
            "DISCOVERED": 0,
            "SNAPSHOT_CAPTURED": 1,
            "ANALYZED": 2,
            "PREDICTED": 3,
            "SETTLED": 4,
            "CLOSING_PRICE_CAPTURED": 5,
        }
        rows = [
            {
                "match_id": identifier,
                "match": labels.get(identifier) or identifier,
                "stages": sorted(values, key=order.get),
                "validation_ready": {"PREDICTED", "SETTLED"}.issubset(values),
            }
            for identifier, values in stages.items()
        ]
        rows.sort(
            key=lambda row: (row["validation_ready"], len(row["stages"]), row["match_id"]), reverse=True
        )
        return {
            "matches": len(rows),
            "validation_ready": sum(row["validation_ready"] for row in rows),
            "rows": rows,
        }

    @staticmethod
    def _agent_coverage(analyses: list[dict]) -> dict:
        rows = []
        total = len(analyses)
        for agent_id in AGENT_REGISTRY:
            statuses = Counter()
            evidence = 0
            for run in analyses:
                result = run.get("results", {}).get(agent_id)
                if not result:
                    statuses["NOT_RUN"] += 1
                    continue
                statuses[result.get("status", "UNKNOWN")] += 1
                evidence += len(result.get("evidence") or [])
            usable = statuses["OK"] + statuses["PARTIAL"]
            rows.append(
                {
                    "agent_id": agent_id,
                    "runs": total - statuses["NOT_RUN"],
                    "usable": usable,
                    "coverage": usable / total if total else 0.0,
                    "evidence": evidence,
                    "statuses": dict(sorted(statuses.items())),
                }
            )
        return {"analysis_runs": total, "agents": rows}

    def _validation_summary(self, validations: list[dict], predictions: list[dict]) -> dict:
        groups = Counter()
        for row in validations:
            groups[row.get("validation_status", "UNKNOWN")] += 1
        settled_groups = defaultdict(list)
        for row in predictions:
            settled_groups[(row["model_id"], row["model_version"], row["market"])].append(row)
        measured = []
        for (model, version, market), rows in sorted(settled_groups.items()):
            assessment = validate_model(
                [row["probability"] for row in rows],
                [row["outcome"] for row in rows],
                min_sample=self.settings.min_sample,
                max_brier=self.settings.max_brier,
                max_calibration_error=self.settings.suspend_calibration_error,
                degraded_calibration_error=self.settings.max_calibration_error,
            )
            measured.append(
                {
                    "model_id": model,
                    "model_version": version,
                    "market": market,
                    "status": assessment.status,
                    "sample_size": assessment.sample_size,
                    "brier_score": assessment.brier,
                    "log_loss": assessment.log_loss,
                    "calibration_error": assessment.calibration_error,
                }
            )
        return {
            "records": len(validations),
            "by_status": dict(sorted(groups.items())),
            "settled_predictions": len(predictions),
            "measured_groups": measured,
            "note": "Only settled out-of-sample predictions can support model admission.",
        }

    def _storage_report(self, generated_at: datetime) -> dict:
        categories = {
            "database": (self.root / "data", lambda path: path.suffix in {".db", ".sqlite", ".sqlite3"}),
            "raw": (self.root / "data" / "raw", lambda _path: True),
            "logs": (self.root / "data" / "logs", lambda _path: True),
            "api_control": (self.root / "data" / "api-control", lambda _path: True),
            "cache": (self.root / ".cache", lambda _path: True),
        }
        details = {}
        def file_stats(directory, predicate=lambda _path: True):
            found = {}
            for path in self._files(directory):
                if not predicate(path):
                    continue
                try:
                    found[path] = path.stat()
                except OSError:
                    # Temporary SQLite journals/cache files can disappear during a scan.
                    continue
            return found

        for name, (directory, predicate) in categories.items():
            files = file_stats(directory, predicate)
            details[name] = {"bytes": sum(stat.st_size for stat in files.values()), "files": len(files)}
        total = sum(stat.st_size for stat in file_stats(self.root).values())
        raw_cutoff = generated_at - timedelta(days=self.raw_retention_days)
        cache_cutoff = generated_at - timedelta(days=self.cache_retention_days)
        candidates = []
        for kind, directory, cutoff in (
            ("raw", self.root / "data" / "raw", raw_cutoff),
            ("cache", self.root / ".cache", cache_cutoff),
        ):
            for path, stat in file_stats(directory).items():
                modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                if modified < cutoff:
                    candidates.append(
                        {
                            "kind": kind,
                            "path": path.relative_to(self.root).as_posix(),
                            "bytes": stat.st_size,
                        }
                    )
        max_bytes = int(self.max_storage_gb * 1024**3)
        return {
            "root": str(self.root),
            "bytes": total,
            "gigabytes": round(total / 1024**3, 4),
            "limit_bytes": max_bytes,
            "used_fraction": min(total / max_bytes, 1.0) if max_bytes else 0.0,
            "within_limit": total <= max_bytes,
            "categories": details,
            "retention_candidates": {
                "files": len(candidates),
                "bytes": sum(item["bytes"] for item in candidates),
                "items": sorted(candidates, key=lambda item: item["bytes"], reverse=True)[:100],
            },
        }

    @staticmethod
    def _files(directory: Path) -> list[Path]:
        if not directory.exists():
            return []
        files = []
        for path in directory.rglob("*"):
            try:
                if path.is_file():
                    files.append(path)
            except OSError:
                continue
        return files


class DataPipelineCollector:
    """Collect one structured snapshot for each due checkpoint of selected matches."""

    def __init__(self, runtime, *, clock=None):
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        self.loop_active = False
        self.running = False
        self.started_at = self.completed_at = self.heartbeat_at = None
        self.last_errors = []
        self.last_summary = None
        self._event_sequence = 0

    def status(self):
        now = self.clock()
        policy = DataPipelineService(self.runtime.repository, settings=self.runtime.settings)
        age = (now - self.heartbeat_at).total_seconds() if self.heartbeat_at else None
        recent = age is not None and 0 <= age <= policy.poll_seconds * 2
        active = bool(recent and (self.loop_active or self.running))
        status = (
            "STALE" if (self.loop_active or self.running) and not recent
            else "STOPPED" if not active
            else "DEGRADED" if self.last_errors
            else "RUNNING" if self.running else "OK"
        )
        return {
            "status": status, "active": active, "healthy": active and not self.last_errors,
            "running": self.running, "poll_seconds": policy.poll_seconds,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "heartbeat_age_seconds": age, "errors": self.last_errors,
            "error_count": len(self.last_errors), "last_summary": self.last_summary,
        }

    def _record_cycle(self, event):
        self._event_sequence += 1
        with self.runtime.repository.engine.begin() as connection:
            self.runtime.repository._append(
                connection, "annotations", identifier=f"collector:{time_ns():020d}:{monotonic_ns():020d}:{self._event_sequence:020d}:{uuid4()}",
                entity_id="collector:lifecycle", match_id=None,
                payload={"event": event, "at": self.clock().isoformat(), **self.status()},
            )

    async def collect_due(self):
        async with self._cycle_lock:
            self.running = True
            self.started_at = self.heartbeat_at = self.clock()
            self._record_cycle("STARTED")
            try:
                summary = await self._collect_due()
                self.last_summary = {key: len(summary[key]) for key in ("captured", "skipped", "failed")}
                self.last_errors = [*summary["failed"], *summary["results"]["failed"]]
                for part in (summary["enrollment"], summary["historical_archive"]):
                    if part.get("error"):
                        self.last_errors.append(part)
                if summary["salabet_error"]:
                    self.last_errors.append(summary["salabet_error"])
                self.last_errors.extend(summary.get("timing_comparison", {}).get("failed", []))
                if summary.get("automatic_multiples", {}).get("error"):
                    self.last_errors.append(summary["automatic_multiples"]["error"])
                return summary
            except Exception as error:
                self.last_errors = [failure_detail(error, stage="COLLECTOR_CYCLE")]
                raise
            finally:
                self.running = False
                self.completed_at = self.heartbeat_at = self.clock()
                self._record_cycle("FAILED" if self.last_errors else "COMPLETED")

    async def _collect_due(self) -> dict:
        async with self._lock:
            now = self.clock()
            from better.domain.models import Match
            from better.services.validation_cohort import ValidationCohortService

            cohort = ValidationCohortService(self.runtime, clock=self.clock)
            try:
                enrollment = await cohort.auto_enroll(now)
            except Exception as error:
                enrollment = {
                    "discovered": 0,
                    "enrolled": 0,
                    "excluded": 0,
                    "error": failure_detail(error, stage="ENROLLMENT"),
                }
            selected = self.runtime.repository.active_collection_selections(now)
            if self.runtime.settings.active_competitions:
                selected = [r for r in selected if r["match"]["competition"] in self.runtime.settings.active_competitions]
            cohort_rows = (
                cohort.active_pre_match(now) if self.runtime.settings.validation_cohort_enabled else []
            )
            cohort_ids = {row["match_id"] for row in cohort_rows}
            targets = {row["match"]["match_id"]: row for row in selected}
            for row in cohort_rows:
                targets.setdefault(row["match_id"], {"match": row["match"]})
            from better.providers.api_football import APIFootballProvider
            from better.services.football_archive import collect_archive

            provider = self.runtime.providers.stats
            candidates = (provider.primary, provider.secondary) if hasattr(provider, "primary") else (provider,)
            archive_provider = next((p for p in candidates if isinstance(p, APIFootballProvider)), None)
            try:
                archive_result = await provider.refresh_archive(
                    matches=[Match.model_validate(row["match"]) for row in targets.values()]
                ) if hasattr(provider, "refresh_archive") else await collect_archive(
                    archive_provider, matches=[Match.model_validate(row["match"]) for row in targets.values()]
                ) if archive_provider else {"status": "DISABLED"}
            except Exception as error:
                archive_result = {"status": "ERROR", "error": failure_detail(error, stage="ARCHIVE")}
            from better.services.salabet_service import SalaBetService
            from better.services.timing_comparison import TimingComparisonService
            timing_targets = {row["match"]["match_id"]: row["match"] for row in selected}
            for candidate in SalaBetService(self.runtime.repository, clock=self.clock).list_candidates():
                if candidate.match_id in targets:
                    timing_targets.setdefault(candidate.match_id, targets[candidate.match_id]["match"])
                else:
                    timing_targets.setdefault(candidate.match_id, {"match_id": candidate.match_id,
                        "competition": candidate.competition_id, "home_team": candidate.home_team,
                        "season": str(candidate.kickoff.year if candidate.kickoff.month >= 7 else candidate.kickoff.year - 1),
                        "away_team": candidate.away_team, "kickoff": candidate.kickoff.isoformat(),
                        "home_team_id": candidate.team_ids[0], "away_team_id": candidate.team_ids[1]})
            timing_result = await TimingComparisonService(self.runtime, clock=self.clock).collect_due(timing_targets.values())
            from better.services.automatic_multiples import AutomaticMultiples
            try:
                multiples_result = AutomaticMultiples(self.runtime, clock=self.clock).collect()
            except Exception as error:
                multiples_result = {"created": [], "error": failure_detail(error, stage="AUTOMATIC_MULTIPLES")}
            captured, skipped, failed = [], [], []
            for selection in targets.values():
                match_id = selection["match"]["match_id"]
                checkpoint = None
                self.heartbeat_at = self.clock()
                try:
                    checkpoint = checkpoint_for(Match.model_validate(selection["match"]).kickoff, now)
                    match = await self.runtime.providers.fixtures.get_match(match_id)
                    checkpoint = checkpoint_for(match.kickoff, now)
                    if self.runtime.repository.has_historical_checkpoint(match_id, checkpoint):
                        skipped.append({"match_id": match_id, "checkpoint": checkpoint})
                        if match_id in cohort_ids:
                            cohort.create_canonical_predictions(match, now=now)
                        continue
                    _, snapshots, failures = await self.runtime.collector.collect(match, now, live=True)
                    if match_id in cohort_ids:
                        researched = await cohort.research_stats_if_needed(
                            match, snapshots, checkpoint, self.clock()
                        )
                        if researched is not None:
                            snapshots["stats"] = researched
                            failures.pop("stats", None)
                    collected_at = self.clock()
                    if collected_at >= match.kickoff:
                        raise ValueError("Collection completed after kickoff")
                    lineup = snapshots.get("lineups")
                    official = bool(lineup and lineup.status == "CONFIRMED_LINEUP")
                    checkpoint = checkpoint_for(match.kickoff, collected_at, official_lineup=official)
                    if self.runtime.repository.has_historical_checkpoint(match_id, checkpoint):
                        skipped.append({"match_id": match_id, "checkpoint": checkpoint})
                        continue
                    observation_id = self.runtime.repository.archive_pre_match_observation(
                        match=match,
                        as_of=collected_at,
                        collected_at=collected_at,
                        snapshots=snapshots,
                        failures=failures,
                        purpose="AUTOMATIC_CHECKPOINT",
                        checkpoint=checkpoint,
                    )
                    captured.append({"match_id": match_id, "checkpoint": checkpoint})
                    if match_id in cohort_ids:
                        cohort.mark_snapshot(match, checkpoint, observation_id)
                        cohort.create_canonical_predictions(match, now=collected_at)
                except Exception as error:
                    diagnostic = failure_detail(error, match_id=match_id, checkpoint=checkpoint,
                                                stage="PREMATCH_CHECKPOINT")
                    failed.append({"match_id": match_id, "reason": diagnostic["code"], "diagnostic": diagnostic})
                    if match_id in cohort_ids:
                        try:
                            match = Match.model_validate(selection["match"])
                            cohort._event(
                                match,
                                "PREDICTION_FAILED",
                                diagnostic["code"],
                                eligibility_status="VALIDATION_PENDING",
                                details={"diagnostic": diagnostic, "checkpoint": checkpoint},
                            )
                        except ValueError:
                            pass
            results = await cohort.poll_results(self.clock())
            from better.services.salabet_service import SalaBetService

            try:
                salabet_settled = SalaBetService(self.runtime.repository, clock=self.clock).settle_verified()
                salabet_error = None
            except Exception as error:
                salabet_settled = []
                salabet_error = failure_detail(error, stage="SALABET_SETTLEMENT")
            return {
                "selected": len(selected),
                "cohort": len(cohort_rows),
                "enrollment": enrollment,
                "captured": captured,
                "skipped": skipped,
                "failed": failed,
                "results": results,
                "salabet_settled": salabet_settled,
                "salabet_error": salabet_error,
                "historical_archive": archive_result,
                "timing_comparison": timing_result,
                "automatic_multiples": multiples_result,
            }

    async def run(self):
        settings = DataPipelineService(self.runtime.repository, settings=self.runtime.settings)
        self.loop_active = True
        try:
            while True:
                if settings.auto_collect:
                    try:
                        await self.collect_due()
                    except Exception:
                        # Failure is recorded by collect_due; keep the next cycle alive.
                        pass
                await asyncio.sleep(settings.poll_seconds)
        finally:
            self.loop_active = False
