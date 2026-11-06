"""Append-only Serie A T24 analyses and candidates; never writes bank rows."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from better.domain.models import Match
from better.domain.schemas import AnalysisRequest
from better.observability.diagnostics import failure_detail
from better.storage.db import TABLES
from better.storage.repository import canonical_hash

ENTITY = "timing-comparison:v1"
WINDOWS = {"T24": timedelta(hours=24)}
GRACE = timedelta(minutes=10)


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TimingComparisonService:
    def __init__(self, runtime=None, *, repository=None, settings=None, clock=None):
        self.runtime = runtime
        self.repository = repository or runtime.repository
        self.settings = settings or runtime.settings
        self.clock = clock or (lambda: datetime.now(UTC))
        from better.services.automation_control import AutomationControl
        self.control = AutomationControl(self.repository, self.settings, clock=self.clock)

    def rows(self):
        return self.repository.all_records("annotations", entity_id=ENTITY)

    def _id(self, match, checkpoint):
        # Schedule changes create a distinct experiment, never a replacement.
        return canonical_hash({"match": match.match_id, "kickoff": match.kickoff.isoformat(), "checkpoint": checkpoint})

    def _append(self, identifier, payload, *, connection=None):
        values = dict(identifier=identifier, entity_id=ENTITY,
                      match_id=payload["match_id"], payload=payload)
        if connection is not None:
            self.repository._append(connection, "annotations", **values)
        else:
            with self.repository.engine.begin() as conn:
                self.repository._append(conn, "annotations", **values)

    def _claim(self, match, checkpoint):
        identifier = "timing-claim:" + self._id(match, checkpoint)
        table = TABLES["annotations"]
        try:
            with self.repository.engine.begin() as conn:
                if conn.execute(select(table.c.id).where(table.c.id == identifier)).first():
                    return False
                self._append(identifier, {"kind": "CLAIM", "match_id": match.match_id,
                    "kickoff": match.kickoff.isoformat(), "checkpoint": checkpoint,
                    "at": self.clock().isoformat()}, connection=conn)
            return True
        except IntegrityError:
            return False

    @staticmethod
    def preselected(run):
        return bool(run.forecast and any(
            row.probabilities and row.method == "RAW_POISSON"
            and row.sample_size and row.reason not in {
                "MODEL_INPUT_INSUFFICIENT", "MODEL_INPUT_FEATURES_INSUFFICIENT"
            } for row in run.forecast.markets))

    def store(self, match, checkpoint, run):
        end = self.clock()
        # Preserve the historical reader contract; scheduling uses T24 only.
        scheduled = match.kickoff - (timedelta(minutes=30) if checkpoint == "T30" else WINDOWS[checkpoint])
        if not (scheduled <= run.as_of <= end < min(match.kickoff, scheduled + GRACE)) or run.created_at > end:
            raise ValueError("TIMING_WINDOW_MISSED: acquisizione fuori finestra pre-match")
        if run.match_id != match.match_id or run.snapshot.match.kickoff != match.kickoff:
            raise ValueError("TIMING_IDENTITY_CONFLICT")
        snapshots = [run.snapshot.stats, run.snapshot.news, run.snapshot.lineups]
        for snapshot in filter(None, snapshots):
            if snapshot.captured_at > run.as_of or any(not p.available_at(run.as_of) for p in snapshot.provenance):
                raise ValueError("TIMING_LEAKAGE_REJECTED")
        for quote in run.snapshot.odds.quotes if run.snapshot.odds else ():
            if quote.captured_at > run.as_of or not quote.provenance.available_at(run.as_of):
                raise ValueError("TIMING_LEAKAGE_REJECTED")
        markets = []
        prediction = run.results.get("predictive_model")
        for model in getattr(getattr(prediction, "payload", None), "model_predictions", ()):
            if model.data_cutoff > run.as_of:
                raise ValueError("TIMING_LEAKAGE_REJECTED")
            for market, probabilities in model.probabilities.items():
                if not model.analysis_available:
                    continue
                leading = max(probabilities, key=probabilities.get)
                quotes = [q for q in run.snapshot.odds.quotes
                          if q.market.type == market and q.market.selection == leading] if run.snapshot.odds else []
                quote = max(quotes, key=lambda q: q.odds, default=None)
                markets.append({"model": model.model, "version": model.model_version,
                    "market": market.value, "probabilities": probabilities, "selection": leading,
                    "odds": float(quote.odds) if quote else None,
                    "bookmaker": quote.bookmaker if quote else None})
        research = [run.input_manifest.get(key) for key in ("research_result", "football_data_result")]
        paid = [r for r in research if r and not r.get("cached")
                and (r.get("response_id") or r.get("session_id") or r.get("input_tokens"))]
        usage = run.input_manifest.get("automation_research_usage")
        payload = {"kind": "CAPTURE", "match_id": match.match_id,
            "kickoff": match.kickoff.isoformat(), "checkpoint": checkpoint,
            "match": match.model_dump(mode="json"),
            "at": end.isoformat(), "as_of": run.as_of.isoformat(),
            "analysis_id": str(run.analysis_id), "markets": markets,
            "preselected": self.preselected(run), "decision": run.decision.status.value,
            "decision_market": run.decision.market.model_dump(mode="json") if run.decision.market else None,
            "lineup_status": run.snapshot.lineups.status if run.snapshot.lineups else "UNKNOWN",
            "lineup_confirmation_pending": not run.snapshot.lineups or run.snapshot.lineups.status != "CONFIRMED_LINEUP",
            "missing": run.input_manifest.get("failures", {}),
            "calls": usage["calls"] if usage else len(paid),
            "cost": usage["known_cost_usd"] if usage else sum(float(r.get("estimated_cost") or 0) for r in paid),
            "cost_known": usage["cost_known"] if usage else all(r.get("estimated_cost") is not None for r in paid),
            "sources": [p.model_dump(mode="json") for s in filter(None, snapshots) for p in s.provenance]
                + [q.provenance.model_dump(mode="json") for q in run.snapshot.odds.quotes] if run.snapshot.odds else
                [p.model_dump(mode="json") for s in filter(None, snapshots) for p in s.provenance]}
        self._append("timing-capture:" + self._id(match, checkpoint), payload)
        return payload

    def view(self, match_id, kickoff, *, candidate=None):
        rows = [r for r in self.rows() if r["match_id"] == match_id and moment(r["kickoff"]) == moment(kickoff)]
        captures = {r["checkpoint"]: r for r in rows if r["kind"] == "CAPTURE"}
        checkpoints = {}
        for name, offset in WINDOWS.items():
            due = moment(kickoff) - offset
            capture = captures.get(name)
            failure = next((r for r in rows if r["checkpoint"] == name and r["kind"] == "FAILED"), None)
            claimed = any(r["checkpoint"] == name and r["kind"] == "CLAIM" for r in rows)
            status = ("COMPLETED" if capture and capture["markets"] and not capture["missing"]
                      and not (name == "T30" and capture.get("lineup_confirmation_pending")) else
                      "DATA_MISSING" if capture else "FAILED" if failure else
                      "IN_PROGRESS" if claimed and self.clock() < due + GRACE else
                      "FAILED" if claimed else
                      "MISSING" if self.clock() >= due + GRACE else "SCHEDULED")
            checkpoints[name] = {"status": status, "scheduled_at": due.isoformat(),
                "capture": capture, "error": failure.get("diagnostic") if failure else None}
        return {"enabled": self.control.policy()["enabled"], "mode": "T24_ONLY",
            "checkpoints": checkpoints, "verdict": "INITIAL_ANALYSIS",
            "reason": "Analisi automatica T−24; nessun confronto successivo",
            "paired": False, "paid_research": self.control.report()["paid_research"]}

    def _transfer(self, match, capture):
        from better.domain.salabet import BetCandidateCreate
        from better.services.batch_analysis import BatchAnalysisService
        from better.services.salabet_service import SalaBetService

        identifier = "timing-transfer:" + self._id(match, "T24")
        if any(r.get("transfer_id") == identifier for r in self.rows()):
            return None
        if not self.clock() < match.kickoff <= self.clock() + timedelta(hours=24):
            return None
        run = self.repository.get_run(capture["analysis_id"])
        if run is None:
            raise ValueError("T24_ANALYSIS_NOT_FOUND")
        item, reason = (BatchAnalysisService(self.runtime, clock=self.clock)._best_candidate(run)
                        if run.forecast else (None, "MODEL_INPUT_INSUFFICIENT"))
        payload = {"kind": "TRANSFER", "transfer_id": identifier, "match_id": match.match_id,
            "kickoff": match.kickoff.isoformat(), "checkpoint": "T24", "at": self.clock().isoformat(),
            "analysis_id": capture["analysis_id"], "candidate_id": None, "reason": reason}
        if item:
            candidate = SalaBetService(self.repository, clock=self.clock).create_batch_candidate(
                BetCandidateCreate(analysis_id=item["analysis_id"], prediction_id=item["prediction_id"],
                    market=item["market"], selection=item["selection"]),
                {"ranking_score": item["ranking_score"], "ranking_reasons":
                    ["Automazione Serie A T−24", *item["reasons"]], "batch_status": item["status"],
                 "data_quality": item["data_quality"], "sample_size": item["sample_size"],
                 "score_breakdown": item["score_breakdown"], "blockers": item["blockers"],
                 "automation_checkpoint": "T24"})
            payload["candidate_id"] = str(candidate.id)
        self._append(identifier, payload)
        return payload

    async def collect_due(self, targets):
        self.control.settings = self.settings
        report = {"enabled": self.control.policy()["enabled"], "captured": [], "failed": [], "transferred": []}
        if not report["enabled"]:
            return report
        if self.control.policy().get("persisted"):
            await self.control.ensure_fx()
        allowed = self.settings.timing_comparison_competitions
        targets = {row["match_id"]: row for row in targets}
        for row in self.rows():
            if row["kind"] == "CAPTURE" and row.get("match"):
                targets.setdefault(row["match_id"], row["match"])
        for raw in targets.values():
            match = Match.model_validate(raw)
            if match.competition not in allowed:
                continue
            for name, offset in WINDOWS.items():
                if not self.control.policy()["enabled"]:
                    return report
                due = match.kickoff - offset
                if not (due <= self.clock() < due + GRACE and self.clock() < match.kickoff):
                    continue
                if not self._claim(match, name):
                    continue
                try:
                    shadow = getattr(self.runtime, "shadow", None)
                    if self.settings.system_two_enabled or getattr(getattr(shadow, "settings", None), "enabled", False):
                        raise ValueError("TIMING_PAID_REASONING_NOT_APPROVED")
                    options = {"research_budget": self.control.guard(match, name)} if self.control.policy().get("persisted") else {"cache_only_research": True}
                    run = await self.runtime.analyze(AnalysisRequest(match_id=match.match_id), **options)
                    row = self.store(match, name, run)
                    report["captured"].append({"match_id": match.match_id, "checkpoint": name, "analysis_id": row["analysis_id"]})
                except Exception as error:
                    diagnostic = failure_detail(error, match_id=match.match_id, checkpoint=name, stage="TIMING_COMPARISON")
                    self._append("timing-failed:" + self._id(match, name), {"kind": "FAILED",
                        "match_id": match.match_id, "kickoff": match.kickoff.isoformat(),
                        "checkpoint": name, "at": self.clock().isoformat(), "diagnostic": diagnostic})
                    report["failed"].append(diagnostic)
        for capture in self.rows():
            if capture["kind"] != "CAPTURE" or capture["checkpoint"] != "T24" or not capture.get("match"):
                continue
            match = Match.model_validate(capture["match"])
            if match.competition not in allowed:
                continue
            try:
                transferred = self._transfer(match, capture)
                if transferred and transferred["candidate_id"]:
                    report["transferred"].append(transferred)
            except Exception as error:
                report["failed"].append(failure_detail(error, match_id=match.match_id,
                    checkpoint="T24", stage="T24_CANDIDATE_TRANSFER"))
        return report

    def report(self):
        rows = self.rows()
        captures = [r for r in rows if r["kind"] == "CAPTURE" and r["checkpoint"] == "T24"]
        transfers = [r for r in rows if r["kind"] == "TRANSFER"]
        matches = len({(r["match_id"], r["kickoff"]) for r in captures})
        control = self.control.report()
        return {"enabled": control["enabled"], "scope": "SERIE_A_ONLY", "mode": "T24_ONLY",
            "checkpoints": ["T24"], "control": control, "paid_research": control["paid_research"],
            "added_paid_budget": 0, "proposed_budget": {"per_match": float(control["per_match_eur"]),
                "per_day": float(control["per_day_eur"]), "per_week": float(control["per_week_eur"]),
                "currency": "EUR", "approved": self.control.policy().get("persisted", False)},
            "captures": len(captures), "matches": matches, "metrics": [],
            "transferred": sum(bool(r.get("candidate_id")) for r in transfers),
            "not_eligible": [r for r in transfers if not r.get("candidate_id")],
            "calls": sum(r["calls"] for r in captures), "cost": sum(r["cost"] for r in captures),
            "unknown_cost_captures": sum(not r.get("cost_known", True) for r in captures),
            "cost_per_match": sum(r["cost"] for r in captures) / matches if matches and all(r.get("cost_known", True) for r in captures) else None,
            "failed": [r for r in rows if r["kind"] == "FAILED" and r["checkpoint"] == "T24"][-10:],
            "note": "Analisi Serie A T−24; candidate entro 24 ore dal kickoff. Nessun confronto successivo e nessuna puntata automatica."}
