"""Cost-aware batch orchestration over the unchanged single-match pipeline."""

import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from better.domain.enums import DecisionStatus, MarketType
from better.domain.salabet import BetCandidateCreate
from better.domain.schemas import AnalysisRequest, PredictiveReport, ValueReport
from better.services.salabet_service import SalaBetService
from better.storage.repository import canonical_hash

MARKETS = (MarketType.MATCH_WINNER, MarketType.OVER_UNDER_2_5, MarketType.BTTS)
CRITICAL_EXCLUSIONS = {
    "IDENTITY_MAPPING_MISSING",
    "API_PROVIDER_ERROR",
    "MODEL_INPUT_INSUFFICIENT",
    "QUOTE_MISSING",
    "MARKET_UNSUPPORTED",
}


class BatchAnalysisService:
    def __init__(self, runtime, *, clock=None):
        self.runtime = runtime
        self.repository = runtime.repository
        self.settings = runtime.settings.batch_ranking
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run(self, match_ids, *, filter_used=None, parent_batch_id=None):
        started = self.clock()
        ordered_ids = tuple(dict.fromkeys(str(value) for value in match_ids))
        identity = {
            "started_at": started.isoformat(),
            "match_ids": ordered_ids,
            "filter": filter_used or {},
            "parent_batch_id": str(parent_batch_id) if parent_batch_id else None,
        }
        batch_id = uuid5(NAMESPACE_URL, "bettor:batch:" + canonical_hash(identity))
        self._audit(batch_id, "STARTED", identity)
        screened, rows = await self._screen(ordered_ids, started)
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def analyze(match_id):
            async with semaphore:
                try:
                    run = await self.runtime.analyze(
                        AnalysisRequest(match_id=match_id, markets=MARKETS)
                    )
                    return match_id, run, None
                except Exception as error:
                    return match_id, None, type(error).__name__

        completed = await asyncio.gather(*(analyze(match_id) for match_id in screened))
        ranked = []
        for match_id, run, error in completed:
            if error:
                rows.append(self._failure_row(match_id, "ERROR", error))
                continue
            selected, rejected = self._best_candidate(run)
            if selected is None:
                rows.append(self._run_row(run, "NOT_ELIGIBLE", exclusion=rejected))
            else:
                ranked.append(selected)

        top = self.select_top(ranked, self.settings.max_candidates)
        transferred = []
        desk = SalaBetService(self.repository, clock=self.clock)
        for position, item in enumerate(top, 1):
            item["ranking_position"] = position
            request = BetCandidateCreate(
                analysis_id=item["analysis_id"],
                prediction_id=item["prediction_id"],
                market=item["market"],
                selection=item["selection"],
            )
            candidate = desk.create_batch_candidate(
                request,
                {
                    "batch_id": batch_id,
                    "ranking_position": position,
                    "ranking_score": item["ranking_score"],
                    "ranking_reasons": item["reasons"],
                    "batch_status": item["status"],
                    "data_quality": item["data_quality"],
                    "sample_size": item["sample_size"],
                    "score_breakdown": item["score_breakdown"],
                    "blockers": item["blockers"],
                },
            )
            item.update(outcome="TRANSFERRED", candidate_id=str(candidate.id))
            self._candidate_audit(batch_id, candidate.id, item)
            transferred.append(item)
        selected_keys = {(item["match_id"], item["market"], item["selection"]) for item in top}
        selected_identity = {id(item) for item in top}
        for item in ranked:
            if id(item) in selected_identity:
                continue
            item.update(outcome="NOT_SELECTED", exclusion="OUTSIDE_TOP_6")
        rows.extend(ranked)
        finished = self.clock()
        report = {
            "batch_id": str(batch_id),
            "parent_batch_id": str(parent_batch_id) if parent_batch_id else None,
            "started_at": started.isoformat(),
            "completed_at": finished.isoformat(),
            "filter": filter_used or {},
            "total": len(ordered_ids),
            "analyzed": len(completed),
            "completed": sum(run is not None for _, run, _ in completed),
            "failed": sum(error is not None for _, _, error in completed),
            "excluded": sum(row["outcome"] in {"EXCLUDED", "NOT_ELIGIBLE"} for row in rows),
            "candidates": len(ranked),
            "transferred": len(transferred),
            "rows": sorted(
                rows,
                key=lambda row: (
                    row.get("ranking_position") is None,
                    row.get("ranking_position") or 999,
                    row.get("match_id", ""),
                ),
            ),
            "selected_keys": sorted(selected_keys),
            "ranking_config": self.settings.model_dump(mode="json"),
        }
        if parent_batch_id:
            previous = self.get(parent_batch_id)
            old = {row["match_id"]: row.get("ranking_score") for row in previous.get("rows", ())}
            for row in report["rows"]:
                before = old.get(row["match_id"])
                after = row.get("ranking_score")
                row["change"] = (
                    "NO_LONGER_ELIGIBLE"
                    if before is not None and after is None
                    else "NEW"
                    if before is None
                    else "IMPROVED"
                    if after > before
                    else "WORSENED"
                    if after < before
                    else "UNCHANGED"
                )
        self._audit(batch_id, "COMPLETED", report)
        return report

    async def _screen(self, match_ids, as_of):
        accepted, rows = [], []
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def one(match_id):
            async with semaphore:
                try:
                    match = await self.runtime.providers.fixtures.get_match(match_id)
                    if match.kickoff <= as_of:
                        return None, self._failure_row(match_id, "EXCLUDED", "KICKOFF_PASSED", match)
                    stats, odds = await asyncio.gather(
                        self.runtime.providers.stats.get_stats(match_id, as_of),
                        self.runtime.providers.odds.get_odds(match_id, as_of),
                    )
                    supported = [quote for quote in odds.quotes if quote.market.type in MARKETS]
                    if not supported:
                        return None, self._failure_row(match_id, "EXCLUDED", "QUOTE_MISSING", match)
                    if stats.home.sample_size < 1 or stats.away.sample_size < 1:
                        return None, self._failure_row(
                            match_id, "EXCLUDED", "MODEL_INPUT_INSUFFICIENT", match
                        )
                    return match_id, None
                except Exception as error:
                    message = str(error).upper()
                    reason = (
                        "ARCHIVE_DATA_UNAVAILABLE"
                        if getattr(getattr(error, "detail", None), "source", None) == "local_archive"
                        else
                        "IDENTITY_MAPPING_MISSING"
                        if "IDENTITY_MAPPING" in message or "IDENTITA SQUADRE" in message
                        else "API_PROVIDER_ERROR"
                    )
                    return None, self._failure_row(match_id, "EXCLUDED", reason)

        results = await asyncio.gather(*(one(match_id) for match_id in match_ids))
        for match_id, row in results:
            if match_id:
                accepted.append(match_id)
            if row:
                rows.append(row)
        return accepted, rows

    def _best_candidate(self, run):
        choices, reason = self.ranked_candidates(run)
        return (max(choices, key=lambda row: (row["ranking_score"], row["selection"])), None) if choices else (None, reason)

    def ranked_candidates(self, run):
        reason_codes = set(run.decision.reason_codes)
        raw_forecasts = tuple(
            item for item in run.forecast.markets if item.method == "RAW_POISSON"
        )
        reduced_sample_only = bool(raw_forecasts) and all(
            item.reason == "MODEL_INPUT_SAMPLE_BELOW_OPERATIONAL_MINIMUM"
            and item.probabilities
            and item.sample_size is not None
            and item.sample_size > 0
            for item in raw_forecasts
        )
        critical = sorted(reason_codes & CRITICAL_EXCLUSIONS)
        if reduced_sample_only and "MODEL_INPUT_INSUFFICIENT" in critical:
            critical.remove("MODEL_INPUT_INSUFFICIENT")
        if critical or any("AMBIGUOUS" in code or "CONFLICT" in code for code in reason_codes):
            return [], ",".join(critical or sorted(reason_codes))
        value_result = run.results.get("value_bettor")
        report = value_result.payload if value_result and isinstance(value_result.payload, ValueReport) else None
        predictive_result = run.results.get("predictive_model")
        predictive = (
            predictive_result.payload
            if predictive_result and isinstance(predictive_result.payload, PredictiveReport)
            else None
        )
        if report is None or predictive is None:
            return [], "MODEL_INPUT_INSUFFICIENT"
        choices = []
        for candidate in (*report.candidates, *report.indicative_candidates):
            if candidate.edge <= 0 or candidate.expected_value <= 0:
                continue
            forecast = next(
                (
                    item
                    for item in run.forecast.markets
                    if item.method == "RAW_POISSON" and item.market == candidate.market.type
                ),
                None,
            )
            prediction = next(
                (
                    item
                    for item in predictive.model_predictions
                    if item.model == candidate.model_id
                    and item.model_version == candidate.model_version
                    and candidate.market.selection
                    in item.probabilities.get(candidate.market.type, {})
                ),
                None,
            )
            if (
                forecast is None
                or prediction is None
                or (
                    forecast.reason.startswith("MODEL_INPUT_")
                    and forecast.reason != "MODEL_INPUT_SAMPLE_BELOW_OPERATIONAL_MINIMUM"
                )
            ):
                continue
            exact_bet = bool(
                run.decision.status == DecisionStatus.BET
                and run.decision.market == candidate.market
            )
            if candidate.operationally_eligible and not exact_bet:
                continue
            choices.append(self._score(run, candidate, forecast, prediction, exact_bet))
        return choices, None if choices else "NO_POSITIVE_VALUE_CANDIDATE"

    def _score(self, run, candidate, forecast, prediction, exact_bet):
        weights = self.settings
        quality = run.decision.data_quality
        probability_robustness = candidate.conservative_probability / candidate.model_probability
        price_coherence = max(
            0, 1 - abs(float(candidate.current_odds) - candidate.fair_odds) / candidate.fair_odds
        )
        components = {
            "probability_robustness": probability_robustness * weights.probability_weight,
            "price_coherence": price_coherence * weights.price_coherence_weight,
            "edge": min(max(candidate.edge, 0) / weights.edge_reference, 1) * weights.edge_weight,
            "expected_value": min(max(candidate.expected_value, 0) / weights.expected_value_reference, 1)
            * weights.expected_value_weight,
            "data_quality": quality.score * weights.data_quality_weight,
            "input_completeness": forecast.input_completeness * weights.completeness_weight,
            "freshness": quality.freshness * weights.freshness_weight,
            "source_reliability": quality.source_reliability * weights.source_reliability_weight,
        }
        penalties = {}
        if forecast.validation_sample_size < forecast.validation_minimum:
            penalties["validator_insufficient"] = weights.validator_penalty * (
                1 - forecast.validation_sample_size / forecast.validation_minimum
            )
        if "LINEUP_NOT_PUBLISHED_YET" in run.decision.reason_codes:
            penalties["lineup_missing"] = weights.lineup_penalty
        if "STATS_PARTIAL" in run.decision.reason_codes:
            penalties["stats_partial"] = weights.partial_stats_penalty
        sample = forecast.sample_size or 0
        if sample < self.runtime.settings.min_training_sample:
            penalties["small_sample"] = weights.small_sample_penalty * (
                1 - sample / self.runtime.settings.min_training_sample
            )
        score = round(max(0, min(100, sum(components.values()) - sum(penalties.values()))), 3)
        reasons = [
            "Buon rapporto probabilita/quota" if candidate.edge > 0 else "Quota senza edge positivo",
            "Qualita dati elevata" if quality.score >= 0.75 else "Qualita dati limitata",
        ]
        if "validator_insufficient" in penalties:
            reasons.append(
                f"Indicativa: Validator {forecast.validation_sample_size}/{forecast.validation_minimum}"
            )
        if "small_sample" in penalties:
            reasons.append(f"Campione ridotto {sample}/{self.runtime.settings.min_training_sample}")
        if "lineup_missing" in penalties:
            reasons.append("Penalizzata: lineup non ancora pubblicata")
        return {
            "match_id": run.match_id,
            "match": f"{run.snapshot.match.home_team} - {run.snapshot.match.away_team}",
            "competition": run.snapshot.match.competition,
            "kickoff": run.snapshot.match.kickoff.isoformat(),
            "analysis_id": str(run.analysis_id),
            "prediction_id": str(prediction.prediction_id),
            "market": candidate.market.type.value,
            "selection": candidate.market.selection,
            "probability": candidate.model_probability,
            "fair_odds": candidate.fair_odds,
            "odds": float(candidate.current_odds),
            "edge": candidate.edge,
            "expected_value": candidate.expected_value,
            "data_quality": quality.score,
            "input_completeness": forecast.input_completeness,
            "freshness": quality.freshness,
            "source_reliability": quality.source_reliability,
            "sample_size": forecast.sample_size,
            "validation_sample": forecast.validation_sample_size,
            "validation_required": forecast.validation_minimum,
            "validation_status": forecast.validation_status.value,
            "status": "BET" if exact_bet else "INDICATIVE",
            "ranking_score": score,
            "score_breakdown": {
                "components": {key: round(value, 3) for key, value in components.items()},
                "penalties": {key: round(value, 3) for key, value in penalties.items()},
            },
            "reasons": reasons[:4],
            "blockers": sorted(run.decision.reason_codes),
            "outcome": "CANDIDATE",
        }

    def get(self, batch_id):
        rows = self.repository.list_records(
            "annotations", last=1000, entity_id=f"batch-analysis:{batch_id}"
        )
        completed = [row for row in rows if row.get("event") == "COMPLETED"]
        if not completed:
            raise KeyError("batch not found")
        return completed[0]["report"]

    def _audit(self, batch_id, event, value):
        document = {"event": event, "batch_id": str(batch_id)}
        document["report" if event == "COMPLETED" else "request"] = value
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "annotations",
                identifier=f"batch-analysis:{batch_id}:{event.lower()}",
                entity_id=f"batch-analysis:{batch_id}",
                match_id=None,
                payload=document,
            )

    def _candidate_audit(self, batch_id, candidate_id, item):
        document = {
            "event": "BATCH_CANDIDATE",
            "batch_id": str(batch_id),
            "candidate_id": str(candidate_id),
            "ranking_position": item["ranking_position"],
            "ranking_score": item["ranking_score"],
            "ranking_reasons": item["reasons"],
            "batch_status": item["status"],
            "analysis_id": item["analysis_id"],
            "data_quality": item["data_quality"],
            "sample_size": item["sample_size"],
            "score_breakdown": item["score_breakdown"],
            "blockers": item["blockers"],
        }
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "annotations",
                identifier="batch-candidate:" + canonical_hash(document),
                entity_id=f"batch-candidate:{candidate_id}",
                match_id=item["match_id"],
                payload=document,
            )

    @staticmethod
    def _failure_row(match_id, outcome, reason, match=None):
        return {
            "match_id": match_id,
            "match": f"{match.home_team} - {match.away_team}" if match else match_id,
            "competition": match.competition if match else None,
            "kickoff": match.kickoff.isoformat() if match else None,
            "outcome": outcome,
            "exclusion": reason,
            "ranking_score": None,
        }

    @staticmethod
    def _run_row(run, outcome, exclusion):
        return {
            "match_id": run.match_id,
            "match": f"{run.snapshot.match.home_team} - {run.snapshot.match.away_team}",
            "competition": run.snapshot.match.competition,
            "kickoff": run.snapshot.match.kickoff.isoformat(),
            "analysis_id": str(run.analysis_id),
            "outcome": outcome,
            "exclusion": exclusion,
            "ranking_score": None,
        }

    @staticmethod
    def select_top(rows, maximum):
        """Stable, deterministic Top N with at most one candidate per match."""
        ordered = sorted(
            rows,
            key=lambda item: (
                -item["ranking_score"],
                item["match_id"],
                item["market"],
                item["selection"],
            ),
        )
        selected, seen = [], set()
        for item in ordered:
            if item["match_id"] in seen:
                continue
            selected.append(item)
            seen.add(item["match_id"])
            if len(selected) == maximum:
                break
        return selected
