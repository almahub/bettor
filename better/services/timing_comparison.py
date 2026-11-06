"""Append-only paired pre-match experiment; never writes validation or bank rows."""

from datetime import UTC, datetime, timedelta
from statistics import mean

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from better.domain.models import Match
from better.domain.schemas import AnalysisRequest
from better.observability.diagnostics import failure_detail
from better.storage.db import TABLES
from better.storage.repository import canonical_hash

ENTITY = "timing-comparison:v1"
WINDOWS = {"T24": timedelta(hours=24), "T30": timedelta(minutes=30)}
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
        scheduled = match.kickoff - WINDOWS[checkpoint]
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
        a, b = captures.get("T24"), captures.get("T30")
        verdict, reason = "PENDING", "Confronto non ancora disponibile"
        if b and candidate:
            market = candidate.get("market")
            model = next((m for m in b["markets"] if m["market"] == market
                          and m["model"] == candidate.get("model_name")
                          and m["version"] == candidate.get("model_version")), None)
            decision = b["decision_market"] or {}
            if not model:
                verdict, reason = "NOT_ADMITTED", "Input insufficienti per questa proposta"
            elif model["selection"] != candidate.get("selection"):
                verdict, reason = "CHANGED", "Il modello indica una selezione diversa"
            elif b["decision"] == "BET" and decision.get("selection") == candidate.get("selection") and decision.get("type") == market:
                verdict, reason = "CONFIRMED", "Proposta ammessa dalla decisione aggiornata"
            else:
                verdict, reason = "NOT_ADMITTED", "Pronostico disponibile, ma BET operativo non ammesso"
        return {"enabled": self.control.policy()["enabled"],
            "checkpoints": checkpoints, "verdict": verdict, "reason": reason,
            "paired": bool(a and b), "paid_research": self.control.report()["paid_research"]}

    async def collect_due(self, targets):
        self.control.settings = self.settings
        report = {"enabled": self.control.policy()["enabled"], "captured": [], "failed": []}
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
                if name == "T30":
                    previous = self.view(match.match_id, match.kickoff.isoformat())["checkpoints"]["T24"]["capture"]
                    if not previous or not previous["preselected"]:
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
        return report

    def report(self):
        from better.domain.enums import MarketType
        from better.services.validation_cohort import ValidationCohortService
        rows = self.rows()
        captures = [r for r in rows if r["kind"] == "CAPTURE"]
        groups = {}
        for row in captures:
            groups.setdefault((row["match_id"], row["kickoff"]), {})[row["checkpoint"]] = row
        results = self.repository.all_records("match_results")
        metrics = {}
        for (match_id, kickoff), pair in groups.items():
            if set(pair) != set(WINDOWS):
                continue
            if any(r.get("match_id") == match_id and r.get("final_status") == "CONFLICT" for r in results):
                continue
            finals = [r for r in results if r.get("match_id") == match_id
                      and r.get("final_status") == "FINAL" and r.get("source_tier") in {"A", "B"}
                      and r.get("home_score") is not None and r.get("away_score") is not None
                      and moment(kickoff) <= moment(r["verified_at"]) <= self.clock()]
            scores = {(r["home_score"], r["away_score"]) for r in finals}
            if len(scores) != 1:
                continue
            home, away = next(iter(scores))
            for initial in pair["T24"]["markets"]:
                updated = next((m for m in pair["T30"]["markets"] if all(m[k] == initial[k] for k in ("model", "version", "market"))), None)
                if not updated or set(initial["probabilities"]) != set(updated["probabilities"]):
                    continue
                key = (initial["model"], initial["version"], initial["market"])
                actual = ValidationCohortService.actual_selection(MarketType(initial["market"]), home, away)
                bucket = metrics.setdefault(key, {name: [] for name in WINDOWS})
                for name, m in (("T24", initial), ("T30", updated)):
                    probs = m["probabilities"]
                    bucket[name].append({"brier": sum((p - int(s == actual)) ** 2 for s, p in probs.items()),
                        "correct": int(m["selection"] == actual), "confidence": probs[m["selection"]],
                        "odds": m["odds"], "return": (m["odds"] if m["selection"] == actual else 0) - 1 if m["odds"] else None})
        output = []
        for (model, version, market), values in metrics.items():
            item = {"model": model, "version": version, "market": market, "pairs": len(values["T24"])}
            for name, observations in values.items():
                calibration = []
                for low in range(10):
                    members = [r for r in observations if min(int(r["confidence"] * 10), 9) == low]
                    if members:
                        calibration.append({"lower": low / 10, "count": len(members),
                            "probability": mean(r["confidence"] for r in members), "frequency": mean(r["correct"] for r in members)})
                priced = [r for r in observations if r["return"] is not None]
                item[name] = {"brier": mean(r["brier"] for r in observations),
                    "accuracy": mean(r["correct"] for r in observations), "calibration": calibration,
                    "mean_odds": mean(r["odds"] for r in priced) if priced else None,
                    "priced_samples": len(priced), "theoretical_roi": mean(r["return"] for r in priced) if priced else None}
            output.append(item)
        matches = len({(r["match_id"], r["kickoff"]) for r in captures})
        control = self.control.report()
        return {"enabled": control["enabled"], "scope": "SERIE_A_ONLY",
            "control": control, "paid_research": control["paid_research"], "added_paid_budget": 0,
            "proposed_budget": {"per_match": float(control["per_match_eur"]),
                "per_day": float(control["per_day_eur"]), "per_week": float(control["per_week_eur"]),
                "currency": "EUR", "approved": self.control.policy().get("persisted", False)},
            "captures": len(captures), "matches": matches, "metrics": output,
            "calls": sum(r["calls"] for r in captures), "cost": sum(r["cost"] for r in captures),
            "unknown_cost_captures": sum(not r.get("cost_known", True) for r in captures),
            "cost_per_match": sum(r["cost"] for r in captures) / matches if matches and all(r.get("cost_known", True) for r in captures) else None,
            "failed": [r for r in rows if r["kind"] == "FAILED"][-10:],
            "note": "Esperimento separato dal Validator. ROI teorico su singole a quota rilevata; non sono movimenti Banca."}
