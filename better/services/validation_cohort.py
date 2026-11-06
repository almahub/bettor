"""Autonomous, append-only live-forward model validation cohort."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from better import __version__
from better.domain.enums import MarketType, PredictionStatus, ValidationStatus
from better.domain.models import HistoricalPrediction, OddsSnapshot, StatsSnapshot
from better.models.calibration import reliability_curve
from better.models.canonical import (
    FEATURE_SCHEMA_VERSION,
    MODEL_NAME,
    MODEL_VERSION,
    poisson_from_stats,
)
from better.observability.diagnostics import failure_detail
from better.providers.result_identity import (
    DeterministicResultIdentityResolver,
    ResultIdentityStatus,
)
from better.providers.results import MatchResult
from better.storage.repository import canonical_hash
from better.system_zero.math import devig
from better.system_zero.validation import closing_line_value, performance, validate_multiclass_model

EXPECTED_SELECTIONS = {
    MarketType.MATCH_WINNER: ("HOME", "DRAW", "AWAY"),
    MarketType.OVER_UNDER_2_5: ("OVER_2_5", "UNDER_2_5"),
    MarketType.BTTS: ("YES", "NO"),
}
CHECKPOINT_ORDER = {"EARLY": 0, "T24": 1, "T6": 2, "T1": 3, "LINEUP": 4, "CLOSING": 5}


def sample_progress(sample_size: int, minimum: int = 200) -> str:
    if sample_size < 50:
        return "RACCOLTA INIZIALE"
    if sample_size < 100:
        return "CAMPIONE PRELIMINARE"
    if sample_size < minimum:
        return "VALIDAZIONE IN CORSO"
    return "CAMPIONE MINIMO RAGGIUNTO"


class ValidationCohortService:
    def __init__(
        self,
        runtime=None,
        *,
        repository=None,
        settings=None,
        clock=None,
        result_provider=None,
        identity_resolver=None,
    ):
        self.runtime = runtime
        self.repository = repository or runtime.repository
        self.settings = settings or runtime.settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.result_provider = (
            result_provider
            if result_provider is not None
            else getattr(getattr(runtime, "providers", None), "results", None)
        )
        self.identity_resolver = identity_resolver
        if (
            self.identity_resolver is None
            and self.result_provider is not None
            and self.settings.result_identity_resolution_enabled
        ):
            self.identity_resolver = DeterministicResultIdentityResolver(
                self.repository,
                self.result_provider,
                kickoff_tolerance_minutes=self.settings.result_identity_kickoff_tolerance_minutes,
                require_competition=self.settings.result_identity_require_competition,
                provider_priority=self.settings.result_provider_priority,
            )

    def _event(self, match, lifecycle_status, reason, *, eligibility_status=None, details=None):
        stable = {
            "match_id": match.match_id,
            "match": match.model_dump(mode="json"),
            "competition_id": match.competition,
            "kickoff": match.kickoff.isoformat(),
            "eligibility_status": eligibility_status,
            "lifecycle_status": lifecycle_status,
            "reason": reason,
            "details": details or {},
        }
        existing = self.repository.all_records("validation_cohort", entity_id=match.match_id)
        latest = max(existing, key=lambda row: row.get("sequence", 0), default=None)
        repeated = next(
            (row for row in existing if {key: row.get(key) for key in stable} == stable),
            None,
        )
        # Milestones stay single events; current outcomes must be allowed to
        # recur after an intervening failure without leaving the old failure active.
        if latest and {key: latest.get(key) for key in stable} == stable:
            return latest
        if repeated and lifecycle_status in {
            "KICKOFF_PASSED", "RESULT_IDENTITY_RESOLVED",
        }:
            return repeated
        document = {**stable, "sequence": len(existing), "at": self.clock().isoformat()}
        identifier = "validation-cohort:" + canonical_hash({**stable, "sequence": len(existing)})
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "validation_cohort",
                identifier=identifier,
                entity_id=match.match_id,
                match_id=match.match_id,
                payload=document,
            )
        return document

    def _verified_competition(self, match):
        if match.competition in self.settings.validation_verified_competitions:
            return True
        allowed = getattr(
            getattr(self.runtime.providers.fixtures, "settings", None), "allowed_competitions", {}
        )
        try:
            native_id = int(match.competition.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return False
        return native_id in allowed

    def enroll(self, match, *, now=None):
        now = now or self.clock()
        if self.settings.active_competitions and match.competition not in self.settings.active_competitions:
            return self._event(match, "VALIDATION_EXCLUDED", "COMPETITION_DISABLED", eligibility_status="VALIDATION_EXCLUDED")
        if match.kickoff.tzinfo is None:
            return self._event(
                match,
                "VALIDATION_EXCLUDED",
                "KICKOFF_INVALID",
                eligibility_status="VALIDATION_EXCLUDED",
            )
        if match.kickoff <= now:
            return self._event(
                match,
                "VALIDATION_EXCLUDED",
                "MATCH_ALREADY_STARTED",
                eligibility_status="VALIDATION_EXCLUDED",
            )
        if not self._verified_competition(match):
            return self._event(
                match,
                "VALIDATION_EXCLUDED",
                "COMPETITION_NOT_VERIFIED",
                eligibility_status="VALIDATION_EXCLUDED",
            )
        self.repository.save_match(match)
        existing = self.repository.all_records("validation_cohort", entity_id=match.match_id)
        if any(row.get("lifecycle_status") == "ENROLLED" for row in existing):
            return next(row for row in existing if row.get("lifecycle_status") == "ENROLLED")
        return self._event(
            match,
            "ENROLLED",
            "VERIFIED_COMPETITION_AND_PREMATCH_IDENTITY",
            eligibility_status="VALIDATION_ELIGIBLE",
        )

    async def auto_enroll(self, now=None):
        now = now or self.clock()
        if not (self.settings.validation_cohort_enabled and self.settings.validation_cohort_auto_enroll):
            return {"discovered": 0, "enrolled": 0, "excluded": 0}
        matches = await self.runtime.providers.fixtures.list_matches(now)
        rows = [self.enroll(match, now=now) for match in matches]
        return {
            "discovered": len(matches),
            "enrolled": sum(row["eligibility_status"] == "VALIDATION_ELIGIBLE" for row in rows),
            "excluded": sum(row["eligibility_status"] == "VALIDATION_EXCLUDED" for row in rows),
        }

    def states(self):
        events = self.repository.all_records("validation_cohort")
        grouped = defaultdict(list)
        for event in events:
            grouped[event["match_id"]].append(event)
        states = []
        for _match_id, rows in grouped.items():
            rows.sort(key=lambda row: row.get("sequence", 0))
            enrolled = next((row for row in rows if row["lifecycle_status"] == "ENROLLED"), rows[0])
            state = dict(rows[-1])
            state["match"] = enrolled["match"]
            state["events"] = len(rows)
            states.append(state)
        return sorted(states, key=lambda row: (row["kickoff"], row["match_id"]))

    def active_pre_match(self, now):
        return [
            row
            for row in self.states()
            if row.get("eligibility_status") != "VALIDATION_EXCLUDED"
            and (not self.settings.active_competitions or row["match"]["competition"] in self.settings.active_competitions)
            and (not self.settings.active_competitions or row["match"]["competition"] in self.settings.active_competitions)
            and datetime.fromisoformat(row["kickoff"]) > now
            and row["lifecycle_status"] not in {"VALIDATION_EXCLUDED", "VALIDATION_INCLUDED"}
        ]

    def mark_snapshot(self, match, checkpoint, observation_id):
        return self._event(
            match,
            "SNAPSHOT_READY",
            "POINT_IN_TIME_CHECKPOINT_CAPTURED",
            eligibility_status="VALIDATION_ELIGIBLE",
            details={"checkpoint": checkpoint, "observation_id": observation_id},
        )

    def _canonical_observation(self, match, now):
        rows = self.repository.all_records("historical_observations", entity_id=match.match_id)
        target = self.settings.validation_canonical_checkpoint
        target_rows = [row for row in rows if row.get("checkpoint") == target]
        if target_rows:
            return max(target_rows, key=lambda row: row["collected_at"]), target, False
        fallback = self.settings.validation_canonical_fallback
        if fallback is None or now >= match.kickoff:
            return None
        from better.services.data_pipeline import checkpoint_for

        current = checkpoint_for(match.kickoff, now)
        if CHECKPOINT_ORDER[current] <= CHECKPOINT_ORDER[target]:
            return None
        fallback_rows = [row for row in rows if row.get("checkpoint") == fallback]
        if not fallback_rows:
            return None
        return max(fallback_rows, key=lambda row: row["collected_at"]), fallback, True

    @staticmethod
    def _complete_prices(odds, market, as_of):
        expected = set(EXPECTED_SELECTIONS[market])
        books = defaultdict(dict)
        for quote in odds.quotes:
            if quote.market.type == market and quote.captured_at <= as_of:
                old = books[quote.bookmaker].get(quote.market.selection)
                if old is None or quote.captured_at > old.captured_at:
                    books[quote.bookmaker][quote.market.selection] = quote
        for bookmaker in sorted(books):
            if set(books[bookmaker]) == expected:
                return bookmaker, books[bookmaker]
        return None

    def create_canonical_predictions(self, match, *, now=None):
        now = now or self.clock()
        if now >= match.kickoff:
            self._event(
                match,
                "VALIDATION_EXCLUDED",
                "POST_KICKOFF_PREDICTION_REJECTED",
                eligibility_status="VALIDATION_EXCLUDED",
            )
            return []
        selected = self._canonical_observation(match, now)
        if selected is None:
            self._event(
                match, "PREDICTION_PENDING", "WAITING_CANONICAL_CHECKPOINT",
                eligibility_status="VALIDATION_PENDING",
                details={"checkpoint": self.settings.validation_canonical_checkpoint},
            )
            return []
        observation, checkpoint, fallback_used = selected
        as_of = datetime.fromisoformat(observation["collected_at"])
        try:
            stats = StatsSnapshot.model_validate(observation["snapshots"]["stats"])
            odds = OddsSnapshot.model_validate(observation["snapshots"]["odds"])
        except (KeyError, ValueError):
            self._event(
                match,
                "DATA_INSUFFICIENT",
                "MINIMUM_STATS_OR_ODDS_MISSING",
                eligibility_status="VALIDATION_PENDING",
                details={"checkpoint": checkpoint},
            )
            return []
        timestamps = [stats.data_cutoff, stats.captured_at]
        timestamps.extend(p.retrieved_at for p in stats.provenance)
        timestamps.extend(p.published_at for p in stats.provenance if p.published_at)
        if any(timestamp > as_of for timestamp in timestamps):
            self._event(
                match,
                "VALIDATION_EXCLUDED",
                "POST_KICKOFF_FEATURE_LEAKAGE_REJECTED",
                eligibility_status="VALIDATION_EXCLUDED",
            )
            return []
        if self.repository.all_records("match_results", entity_id=match.match_id):
            self._event(
                match,
                "VALIDATION_EXCLUDED",
                "RESULT_AVAILABLE_BEFORE_PREDICTION",
                eligibility_status="VALIDATION_EXCLUDED",
            )
            return []
        calculated = poisson_from_stats(stats.home, stats.away, tuple(EXPECTED_SELECTIONS))
        feature_schema_version = self.settings.validation_feature_schema_version or FEATURE_SCHEMA_VERSION
        existing_observations = self.repository.all_records("validation_observations")
        created = []
        for market, probabilities in calculated["probabilities"].items():
            prices = self._complete_prices(odds, market, as_of)
            if prices is None:
                continue
            bookmaker, quotes = prices
            quote_times = []
            for quote in quotes.values():
                quote_times.extend((quote.captured_at, quote.provenance.retrieved_at))
                if quote.provenance.published_at:
                    quote_times.append(quote.provenance.published_at)
            if any(timestamp > as_of for timestamp in quote_times):
                self._event(
                    match,
                    "VALIDATION_EXCLUDED",
                    "POST_KICKOFF_FEATURE_LEAKAGE_REJECTED",
                    eligibility_status="VALIDATION_EXCLUDED",
                )
                return []
            identity = f"{match.match_id}:{MODEL_NAME}:{MODEL_VERSION}:{market.value}"
            if self.repository.all_records("canonical_predictions", entity_id=identity):
                continue
            validation_rows = [
                row
                for row in existing_observations
                if row.get("source_type") == "LIVE_FORWARD"
                and row.get("inclusion_status") == "VALIDATION_INCLUDED"
                and row.get("model_name") == MODEL_NAME
                and row.get("model_version") == MODEL_VERSION
                and row.get("market") == market.value
                and row.get("feature_schema_version") == feature_schema_version
                and row.get("config_hash") == self.settings.version
            ]
            if validation_rows:
                validation_status = ValidationStatus(
                    self._metrics(
                        (
                            MODEL_NAME,
                            MODEL_VERSION,
                            market.value,
                            feature_schema_version,
                            self.settings.version,
                        ),
                        validation_rows,
                    )["validation_status"]
                )
            else:
                validation_status = ValidationStatus.INSUFFICIENT_SAMPLE
            model_input_ok = calculated["sample_size"] >= self.settings.min_training_sample
            operationally_eligible = model_input_ok and validation_status in {
                ValidationStatus.NORMAL_WEIGHT,
                ValidationStatus.REDUCED_WEIGHT,
            }
            prediction_status = (
                PredictionStatus.VALIDATED
                if operationally_eligible
                else PredictionStatus.UNVALIDATED
                if model_input_ok
                else PredictionStatus.INDICATIVE
            )
            reason = (
                "ALL_OPERATIONAL_CHECKS_PASSED"
                if operationally_eligible
                else "MODEL_INPUT_SAMPLE_BELOW_OPERATIONAL_MINIMUM"
                if not model_input_ok
                else "MODEL_VALIDATION_SUSPENDED"
                if validation_status == ValidationStatus.SUSPEND_MODEL
                else "MODEL_VALIDATION_INSUFFICIENT"
            )
            payload = {
                "prediction_id": "canonical-prediction:" + canonical_hash({"identity": identity}),
                "match_id": match.match_id,
                "competition_id": match.competition,
                "kickoff": match.kickoff.isoformat(),
                "as_of": as_of.isoformat(),
                "created_at": now.isoformat(),
                "canonical_checkpoint": checkpoint,
                "fallback_used": fallback_used,
                "model_name": MODEL_NAME,
                "model_version": MODEL_VERSION,
                "feature_schema_version": feature_schema_version,
                "config_hash": self.settings.version,
                "market": market.value,
                "probabilities": probabilities,
                "fair_odds": {selection: 1 / value for selection, value in probabilities.items()},
                "features": calculated["features"],
                "sample_size": calculated["sample_size"],
                "prediction_status": prediction_status.value,
                "analysis_available": True,
                "operationally_eligible": operationally_eligible,
                "reason": reason,
                "input_completeness": 1.0,
                "required_features": ["home_goal_rate", "away_goal_rate"],
                "missing_features": [],
                "validation_sample_size": len(validation_rows),
                "validation_minimum": self.settings.min_sample,
                "validation_status": validation_status.value,
                "data_cutoff": stats.data_cutoff.isoformat(),
                "source_snapshot_ids": [stats.snapshot_id, odds.snapshot_id],
                "entry_odds": {selection: float(quote.odds) for selection, quote in quotes.items()},
                "entry_quote_times": {
                    selection: quote.captured_at.isoformat() for selection, quote in quotes.items()
                },
                "bookmaker": bookmaker,
                "software_version": __version__,
                "source_type": "LIVE_FORWARD",
                "immutable": True,
            }
            with self.repository.engine.begin() as connection:
                self.repository._append(
                    connection,
                    "canonical_predictions",
                    identifier=payload["prediction_id"],
                    entity_id=identity,
                    match_id=match.match_id,
                    payload=payload,
                )
            created.append(payload)
        if created:
            self._event(
                match,
                "CANONICAL_PREDICTION_CREATED",
                "CANONICAL_CHECKPOINT_LOCKED",
                eligibility_status="VALIDATION_ELIGIBLE",
                details={"checkpoint": checkpoint, "markets": [row["market"] for row in created]},
            )
        elif not self.repository.all_records("canonical_predictions", match_id=match.match_id):
            self._event(
                match,
                "DATA_INSUFFICIENT",
                "COMPLETE_SAME_BOOKMAKER_MARKET_MISSING",
                eligibility_status="VALIDATION_PENDING",
            )
        return created

    async def research_stats_if_needed(self, match, snapshots, checkpoint, as_of):
        if "stats" in snapshots or checkpoint not in {
            self.settings.validation_canonical_checkpoint,
            self.settings.validation_canonical_fallback,
        }:
            return None
        research = self.runtime.football_data_research
        if research is None or not research.settings.enabled:
            return None
        if not self._within_research_budget(match.match_id, as_of):
            self._event(
                match,
                "DATA_INSUFFICIENT",
                "VALIDATION_RESEARCH_BUDGET_EXHAUSTED",
                eligibility_status="VALIDATION_PENDING",
            )
            return None
        research_match = match
        fixture = self.runtime.providers.fixtures
        if hasattr(fixture, "competition_names"):
            name = fixture.competition_names().get(match.competition)
            if name:
                research_match = match.model_copy(update={"competition": name})
        from better.services.football_data_research import normalize_football_data

        result = await research.research(research_match, as_of)
        stats = normalize_football_data(match, result)
        charged = not result.cached
        cost_event = {
            "match_id": match.match_id,
            "checkpoint": checkpoint,
            "at": result.completed_at.isoformat(),
            "request_id": result.request_id,
            "agent_id": result.agent_id,
            "session_id": result.session_id,
            "tool_calls": result.tool_calls if charged else 0,
            "search_calls": result.search_calls if charged else 0,
            "input_tokens": result.input_tokens if charged else 0,
            "output_tokens": result.output_tokens if charged else 0,
            "estimated_cost": (result.estimated_cost or 0) if charged else 0,
            "cache_hit": result.cached,
            "research_call_avoided": result.cached,
            "status": result.status.value,
        }
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "validation_cost_events",
                identifier="validation-cost:" + result.request_id + (":cache" if result.cached else ":api"),
                entity_id=match.match_id,
                match_id=match.match_id,
                payload=cost_event,
            )
        return stats

    def _within_research_budget(self, match_id, as_of):
        rows = self.repository.all_records("validation_cost_events")
        day = as_of.date()
        week = as_of.isocalendar()[:2]
        match_cost = sum(float(row.get("estimated_cost", 0)) for row in rows if row["match_id"] == match_id)
        day_cost = sum(
            float(row.get("estimated_cost", 0))
            for row in rows
            if datetime.fromisoformat(row["at"]).date() == day
        )
        week_cost = sum(
            float(row.get("estimated_cost", 0))
            for row in rows
            if datetime.fromisoformat(row["at"]).isocalendar()[:2] == week
        )
        planned = float(
            min(
                Decimal(str(self.settings.validation_research_budget_per_match)),
                getattr(
                    getattr(self.runtime, "football_data_research", None),
                    "settings",
                    type("Budget", (), {"max_cost_per_match": Decimal("0")})(),
                ).max_cost_per_match,
            )
        )
        return (
            match_cost + planned <= self.settings.validation_research_budget_per_match
            and day_cost + planned <= self.settings.validation_research_budget_per_day
            and week_cost + planned <= self.settings.validation_research_budget_per_week
        )

    async def poll_results(self, now=None, *, match_ids=None):
        now = now or self.clock()
        summary = {"pending": [], "verified": [], "settled": [], "conflicts": [], "failed": []}
        if not (self.settings.validation_cohort_enabled and self.settings.validation_result_poll_enabled):
            return summary
        for state in self.states():
            if match_ids is not None and state["match_id"] not in match_ids:
                continue
            if state.get("eligibility_status") == "VALIDATION_EXCLUDED":
                continue
            match = self.repository.get_match_record(state["match_id"])
            if match and self.settings.active_competitions and match.competition not in self.settings.active_competitions:
                continue
            if match is None or now < match.kickoff + timedelta(minutes=90):
                continue
            predictions = self.repository.all_records("canonical_predictions", match_id=match.match_id)
            existing_observations = self.repository.all_records("validation_observations", entity_id=match.match_id)
            settled_ids = {row["prediction_id"] for row in existing_observations}
            if predictions and all(row["prediction_id"] in settled_ids for row in predictions):
                continue
            self._event(
                match, "KICKOFF_PASSED", "REGULATION_TIME_ELAPSED", eligibility_status="VALIDATION_ELIGIBLE"
            )
            verified = self.repository.all_records("match_results", entity_id=match.match_id)
            verified = [row for row in verified if row.get("validation_eligible") is True
                        and row.get("status") == "FINAL" and row.get("source_tier") in {"A", "B"}]
            if verified and self.settings.result_auto_settlement_enabled:
                try:
                    if len({(row["home_score"], row["away_score"]) for row in verified}) != 1:
                        raise ValueError("RESULT_CONFLICT")
                    result = MatchResult.model_validate({k: verified[0][k] for k in MatchResult.model_fields if k in verified[0]})
                    summary["settled"].extend(self.record_result(match, result, now))
                    summary["verified"].append(match.match_id)
                except Exception as error:
                    diagnostic = failure_detail(error, match_id=match.match_id, checkpoint="RESULT",
                                                provider=verified[0].get("provider"), stage="LOCAL_SETTLEMENT")
                    self._event(match, "SETTLEMENT_FAILED", diagnostic["code"],
                                eligibility_status="VALIDATION_PENDING", details={"diagnostic": diagnostic})
                    summary["failed"].append(diagnostic)
                continue
            if self.result_provider is None:
                self._event(
                    match,
                    "RESULT_PENDING",
                    "NO_AUTOMATIC_RESULT_PROVIDER_FOR_IDENTITY",
                    eligibility_status="VALIDATION_PENDING",
                )
                summary["pending"].append(match.match_id)
                continue
            try:
                result_identity = match
                if self.identity_resolver is not None:
                    resolution = await self.identity_resolver.resolve(match, now)
                    if not resolution.verified or resolution.identity is None:
                        lifecycle = (
                            "RESULT_CONFLICT"
                            if resolution.status == ResultIdentityStatus.CONFLICT
                            else "RESULT_PENDING"
                        )
                        self._event(
                            match,
                            lifecycle,
                            f"RESULT_IDENTITY_{resolution.status.value}",
                            eligibility_status="VALIDATION_PENDING",
                            details={
                                "resolution_method": resolution.resolution_method,
                                "confidence": resolution.confidence,
                                "candidate_count": resolution.candidate_count,
                            },
                        )
                        target = "conflicts" if lifecycle == "RESULT_CONFLICT" else "pending"
                        summary[target].append(match.match_id)
                        continue
                    result_identity = resolution.identity
                    self._event(
                        match,
                        "RESULT_IDENTITY_RESOLVED",
                        resolution.status.value,
                        eligibility_status="VALIDATION_PENDING",
                        details={
                            "provider": resolution.identity.provider,
                            "provider_match_id": resolution.identity.provider_match_id,
                            "resolution_method": resolution.resolution_method,
                            "confidence": resolution.confidence,
                        },
                    )
                fetch_claims = getattr(self.result_provider, "get_claims", None)
                fetch_document = {
                    "scope": "result_fetch",
                    "match_id": match.match_id,
                    "attempted_at": now.isoformat(),
                    "provider": getattr(result_identity, "provider", "direct_result_provider"),
                    "provider_match_id": getattr(result_identity, "provider_match_id", match.match_id),
                }
                with self.repository.engine.begin() as connection:
                    self.repository._append(
                        connection,
                        "identity_resolution_attempts",
                        identifier="result-fetch:" + canonical_hash(fetch_document),
                        entity_id=match.match_id,
                        match_id=match.match_id,
                        payload=fetch_document,
                    )
                claims = (
                    tuple(await fetch_claims(result_identity, now))
                    if fetch_claims is not None
                    else (await self.result_provider.get_result(result_identity, now),)
                )
                claims = tuple(claim for claim in claims if claim is not None)
                provider_match_id = getattr(result_identity, "provider_match_id", match.match_id)
                for claim in claims:
                    self.repository.save_result_claim(
                        match_id=match.match_id,
                        provider_match_id=provider_match_id,
                        result=claim,
                    )
                final_claims = [claim for claim in claims if claim.final_status == "FINAL"]
                scores = {(claim.home_score, claim.away_score) for claim in final_claims}
                if len(scores) > 1:
                    self._event(
                        match,
                        "RESULT_CONFLICT",
                        "RELIABLE_RESULT_PROVIDERS_DISAGREE",
                        eligibility_status="VALIDATION_PENDING",
                    )
                    summary["conflicts"].append(match.match_id)
                    continue
                result = final_claims[0] if final_claims else (claims[0] if claims else None)
                if result is not None and result.final_status == "UNVERIFIED" and result.provider_status == "IDENTITY_MISMATCH":
                    self._event(
                        match, "RESULT_CONFLICT", "RESULT_IDENTITY_MISMATCH",
                        eligibility_status="VALIDATION_PENDING",
                        details={"diagnostic": {
                            "code": "RESULT_IDENTITY_MISMATCH", "provider": result.provider,
                            "match_id": match.match_id, "checkpoint": "RESULT",
                            "message": "Competizione, squadre o kickoff del risultato differiscono dal mapping verificato.",
                        }},
                    )
                    summary["conflicts"].append(match.match_id)
                    continue
                if result is None or result.final_status in {"PENDING", "UNVERIFIED"}:
                    self._event(
                        match,
                        "RESULT_PENDING",
                        "VERIFIED_RESULT_NOT_AVAILABLE",
                        eligibility_status="VALIDATION_PENDING",
                    )
                    summary["pending"].append(match.match_id)
                    continue
                if result.final_status == "CONFLICT":
                    self._event(
                        match,
                        "RESULT_CONFLICT",
                        "RELIABLE_RESULT_PROVIDERS_DISAGREE",
                        eligibility_status="VALIDATION_PENDING",
                    )
                    summary["conflicts"].append(match.match_id)
                    continue
                if not self.settings.result_auto_settlement_enabled:
                    self._event(
                        match,
                        "RESULT_VERIFIED",
                        "AUTOMATIC_SETTLEMENT_DISABLED",
                        eligibility_status="VALIDATION_PENDING",
                    )
                    summary["verified"].append(match.match_id)
                    continue
                recorded = self.record_result(match, result, now)
                summary["verified"].append(match.match_id)
                summary["settled"].extend(recorded)
            except Exception as error:
                diagnostic = failure_detail(error, match_id=match.match_id, checkpoint="RESULT",
                                            provider=getattr(self.result_provider, "provider_id", None),
                                            stage="RESULT_RESOLUTION_OR_SETTLEMENT")
                self._event(
                    match,
                    "SETTLEMENT_FAILED",
                    diagnostic["code"],
                    eligibility_status="VALIDATION_PENDING",
                    details={"diagnostic": diagnostic},
                )
                summary["failed"].append(diagnostic)
        return summary

    def record_result(self, match, result, settled_at=None):
        settled_at = settled_at or self.clock()
        if settled_at < match.kickoff + timedelta(minutes=90):
            raise ValueError("Final result cannot precede elapsed regulation time")
        existing = self.repository.all_records("match_results", entity_id=match.match_id)
        score = (result.home_score, result.away_score)
        if existing:
            old_score = (existing[0]["home_score"], existing[0]["away_score"])
            if old_score != score:
                raise ValueError("RESULT_CONFLICT")
        else:
            document = {
                **result.model_dump(mode="json"),
                "status": "FINAL",
                "recorded_at": settled_at.isoformat(),
                "source_type": "AUTOMATIC_PROVIDER",
                "validation_eligible": True,
            }
            with self.repository.engine.begin() as connection:
                self.repository._append(
                    connection,
                    "match_results",
                    identifier=f"match-result:{match.match_id}",
                    entity_id=match.match_id,
                    match_id=match.match_id,
                    payload=document,
                )
        self._event(match, "RESULT_VERIFIED", "TIER_A_OR_B_RESULT", eligibility_status="VALIDATION_ELIGIBLE")
        return self._settle(match, result, settled_at)

    def _closing_quotes(self, match_id, kickoff):
        latest = {}
        for observation in self.repository.all_records(
            "historical_observations", entity_id=match_id
        ):
            odds = observation.get("snapshots", {}).get("odds") or {}
            for quote in odds.get("quotes", []):
                captured = datetime.fromisoformat(quote["captured_at"])
                if captured >= kickoff:
                    continue
                market = quote["market"]
                key = (market["type"], market["selection"], quote["bookmaker"])
                if key not in latest or captured > datetime.fromisoformat(latest[key]["captured_at"]):
                    latest[key] = quote
        return latest

    @staticmethod
    def actual_selection(market, home, away):
        if market == MarketType.MATCH_WINNER:
            return "HOME" if home > away else "AWAY" if away > home else "DRAW"
        if market == MarketType.OVER_UNDER_2_5:
            return "OVER_2_5" if home + away > 2 else "UNDER_2_5"
        if market == MarketType.BTTS:
            return "YES" if home > 0 and away > 0 else "NO"
        raise ValueError("Unsupported validation market")

    def _settle(self, match, result, settled_at):
        predictions = self.repository.all_records("canonical_predictions", match_id=match.match_id)
        if not predictions:
            self._event(match, "VALIDATION_EXCLUDED", "PREMATCH_PREDICTION_MISSING",
                        eligibility_status="VALIDATION_EXCLUDED")
            return []
        existing = {row["prediction_id"] for row in self.repository.all_records(
            "validation_observations", entity_id=match.match_id)}
        closing = self._closing_quotes(match.match_id, match.kickoff)
        settled = []
        for prediction in predictions:
            if prediction["prediction_id"] in existing:
                continue
            if not (
                datetime.fromisoformat(prediction["created_at"]) < match.kickoff
                and datetime.fromisoformat(prediction["as_of"]) <= match.kickoff
                and datetime.fromisoformat(prediction["data_cutoff"])
                <= datetime.fromisoformat(prediction["as_of"])
            ):
                self._event(
                    match,
                    "VALIDATION_EXCLUDED",
                    "LEAKAGE_REJECTED_AT_SETTLEMENT",
                    eligibility_status="VALIDATION_EXCLUDED",
                )
                continue
            market = MarketType(prediction["market"])
            actual = self.actual_selection(market, result.home_score, result.away_score)
            outcomes = {selection: int(selection == actual) for selection in prediction["probabilities"]}
            closing_odds = {
                selection: (closing.get((market.value, selection, prediction["bookmaker"]), {}).get("odds"))
                for selection in prediction["probabilities"]
            }
            observation = {
                **prediction,
                "actual_selection": actual,
                "outcomes": outcomes,
                "settlements": {selection: "WON" if won else "LOST" for selection, won in outcomes.items()},
                "home_score": result.home_score,
                "away_score": result.away_score,
                "result_provider": result.provider,
                "result_source": str(result.source_url),
                "result_source_tier": result.source_tier,
                "settled_at": settled_at.isoformat(),
                "closing_odds": closing_odds,
                "closing_price_status": (
                    "AVAILABLE"
                    if all(value is not None for value in closing_odds.values())
                    else "CLOSING_PRICE_MISSING"
                ),
                "inclusion_status": "VALIDATION_INCLUDED",
            }
            observation_id = "validation-observation:" + prediction["prediction_id"]
            with self.repository.engine.begin() as connection:
                self.repository._append(
                    connection,
                    "validation_observations",
                    identifier=observation_id,
                    entity_id=match.match_id,
                    match_id=match.match_id,
                    payload=observation,
                )
                top = max(prediction["probabilities"], key=prediction["probabilities"].get)
                won = int(top == actual)
                odds = prediction["entry_odds"][top]
                closing_top = closing_odds[top]
                history = HistoricalPrediction(
                    match_id=match.match_id,
                    model_id=prediction["model_name"],
                    model_version=prediction["model_version"],
                    competition=match.competition,
                    market=market,
                    selection=top,
                    probability=prediction["probabilities"][top],
                    outcome=won,
                    predicted_at=datetime.fromisoformat(prediction["created_at"]),
                    settled_at=settled_at,
                    data_cutoff=datetime.fromisoformat(prediction["data_cutoff"]),
                    split="LIVE",
                    odds=odds,
                    closing_odds=closing_top,
                    profit=Decimal(str(odds - 1 if won else -1)),
                    season=match.season,
                    bookmaker=prediction["bookmaker"],
                    source_type="LIVE_FORWARD",
                    feature_schema_version=prediction["feature_schema_version"],
                    config_hash=prediction["config_hash"],
                    canonical_checkpoint=prediction["canonical_checkpoint"],
                    probabilities=prediction["probabilities"],
                    actual_selection=actual,
                    software_version=prediction["software_version"],
                )
                payload = history.model_dump(mode="json")
                self.repository._append(
                    connection,
                    "settled_predictions",
                    identifier="validation-history:" + prediction["prediction_id"],
                    entity_id=f"{history.model_id}:{history.market.value}:{history.selection}",
                    match_id=match.match_id,
                    payload=payload,
                )
            settled.append(observation_id)
        if settled:
            self._event(
                match, "SETTLED", "CANONICAL_MARKETS_SETTLED", eligibility_status="VALIDATION_ELIGIBLE"
            )
            self._event(
                match,
                "VALIDATION_INCLUDED",
                "LIVE_FORWARD_OBSERVATION_ACCEPTED",
                eligibility_status="VALIDATION_ELIGIBLE",
                details={"observations": len(settled)},
            )
        return settled

    def report(self):
        states = self.states()
        hidden_ids = {r["match_id"] for r in self.repository.all_records("annotations", entity_id="cohort:hidden:v1")
                      if r.get("kind") == "HIDDEN"}
        predictions = self.repository.all_records("canonical_predictions")
        observations = self.repository.all_records("validation_observations")
        costs = self.repository.all_records("validation_cost_events")
        all_resolution_attempts = self.repository.all_records("identity_resolution_attempts")
        identity_attempts = [row for row in all_resolution_attempts if row.get("scope") == "result_identity"]
        result_fetch_attempts = [row for row in all_resolution_attempts if row.get("scope") == "result_fetch"]
        identity_mappings = [
            row
            for row in self.repository.all_records("identity_mappings")
            if row.get("scope") == "result_identity"
        ]
        result_claims = self.repository.all_records("match_result_claims")
        verified_results = self.repository.all_records("match_results")
        groups = defaultdict(list)
        for row in observations:
            if (
                row.get("source_type") == "LIVE_FORWARD"
                and row.get("inclusion_status") == "VALIDATION_INCLUDED"
            ):
                groups[
                    (
                        row["model_name"],
                        row["model_version"],
                        row["market"],
                        row["feature_schema_version"],
                        row["config_hash"],
                    )
                ].append(row)
        measured = [self._metrics(key, rows) for key, rows in sorted(groups.items())]
        all_events = self.repository.all_records("validation_cohort")
        status_counts = Counter(row["lifecycle_status"] for row in states)
        event_matches = defaultdict(set)
        for event in all_events:
            event_matches[event["lifecycle_status"]].add(event["match_id"])
        valid = sum(
            row.get("source_type") == "LIVE_FORWARD" and row.get("inclusion_status") == "VALIDATION_INCLUDED"
            for row in observations
        )
        latest_identity = {}
        for row in reversed(identity_attempts):
            latest_identity[row["match_id"]] = row
        mapped_matches = {
            row["source_id"] for row in identity_mappings if row["kind"] == "match" and row.get("identity")
        }
        cohort_match_ids = {row["match_id"] for row in states}
        concluded = {row["match_id"] for row in all_events if row["lifecycle_status"] == "KICKOFF_PASSED"}
        identity_counts = Counter(row["result"] for row in latest_identity.values())
        settlement_latencies = []
        kickoff_by_match = {row["match_id"]: datetime.fromisoformat(row["kickoff"]) for row in states}
        for row in verified_results:
            if row["match_id"] in kickoff_by_match and row.get("recorded_at"):
                settlement_latencies.append(
                    (
                        datetime.fromisoformat(row["recorded_at"]) - kickoff_by_match[row["match_id"]]
                    ).total_seconds()
                    / 60
                )
        return {
            "enabled": self.settings.validation_cohort_enabled,
            "source_type": "LIVE_FORWARD_VALIDATION",
            "canonical_checkpoint": self.settings.validation_canonical_checkpoint,
            "fallback_checkpoint": self.settings.validation_canonical_fallback,
            "minimum_sample": self.settings.min_sample,
            "enrolled": len(event_matches["ENROLLED"]),
            "canonical_predictions": len(predictions),
            "results_pending": status_counts["RESULT_PENDING"],
            "results_verified": len(event_matches["RESULT_VERIFIED"]),
            "settled_matches": len({row["match_id"] for row in observations}),
            "valid_observations": valid,
            "status_counts": dict(status_counts),
            "groups": measured,
            "matches": [r for r in self._match_details(
                states, predictions, observations, identity_mappings, result_claims, verified_results
            ) if r["match_id"] not in hidden_ids],
            "hidden_matches": len(hidden_ids),
            "result_identity": {
                "resolved": identity_counts["VERIFIED_EXACT"] + identity_counts["VERIFIED_MAPPED"],
                "ambiguous": identity_counts["AMBIGUOUS"],
                "not_found": identity_counts["NOT_FOUND"] + identity_counts["PROBABLE"],
                "conflict": identity_counts["CONFLICT"],
            },
            "result_pipeline_metrics": {
                "identity_resolutions_attempted": len(identity_attempts),
                "mappings_reused": sum(
                    row["reason"] == "PERSISTED_MATCH_MAPPING" for row in identity_attempts
                ),
                "exact_matches": identity_counts["VERIFIED_EXACT"],
                "mapped_matches": identity_counts["VERIFIED_MAPPED"],
                "ambiguous": identity_counts["AMBIGUOUS"],
                "not_found": identity_counts["NOT_FOUND"],
                "result_fetch_calls": len(result_fetch_attempts),
                "result_claims": len(result_claims),
                "verified_results": len(verified_results),
                "conflicts": len(event_matches["RESULT_CONFLICT"]),
                "settlements_created": len(observations),
                "validation_observations_created": len(observations),
            },
            "cost_control": {
                "tool_calls": sum(row.get("tool_calls", 0) for row in costs),
                "tokens": sum(row.get("input_tokens", 0) + row.get("output_tokens", 0) for row in costs),
                "estimated_cost": sum(float(row.get("estimated_cost", 0)) for row in costs),
                "cache_hits": sum(bool(row.get("cache_hit")) for row in costs),
                "research_calls_avoided": sum(bool(row.get("research_call_avoided")) for row in costs),
                "cost_per_valid_observation": (
                    sum(float(row.get("estimated_cost", 0)) for row in costs) / valid if valid else None
                ),
            },
            "kpi": {
                "live_forward_observations": valid,
                "leakage_rejections": status_counts["VALIDATION_EXCLUDED"],
                "duplicate_rejections": 0,
                "verified_result_identity_rate": (
                    len(mapped_matches & cohort_match_ids) / len(cohort_match_ids) if cohort_match_ids else 0
                ),
                "automatic_result_rate": (
                    len({row["match_id"] for row in verified_results} & concluded) / len(concluded)
                    if concluded
                    else 0
                ),
                "prediction_to_observation_rate": len(observations) / len(predictions) if predictions else 0,
                "average_kickoff_to_settlement_minutes": (
                    math.fsum(settlement_latencies) / len(settlement_latencies)
                    if settlement_latencies
                    else None
                ),
                "conflicts": len(event_matches["RESULT_CONFLICT"]),
                "ambiguous_rate": (
                    identity_counts["AMBIGUOUS"] / len(latest_identity) if latest_identity else 0
                ),
            },
        }

    def _metrics(self, key, rows):
        model, version, market, feature, config_hash = key
        assessment = validate_multiclass_model(
            [row["probabilities"] for row in rows],
            [row["actual_selection"] for row in rows],
            min_sample=self.settings.min_sample,
            max_brier=self.settings.max_brier,
            max_calibration_error=self.settings.suspend_calibration_error,
            degraded_calibration_error=self.settings.max_calibration_error,
        )
        confidences = [max(row["probabilities"].values()) for row in rows]
        correct = [
            int(max(row["probabilities"], key=row["probabilities"].get) == row["actual_selection"])
            for row in rows
        ]
        labels = EXPECTED_SELECTIONS[MarketType(market)]
        quality = {
            "sample_size": assessment.sample_size,
            "brier_score": assessment.brier,
            "log_loss": assessment.log_loss,
            "calibration": assessment.calibration_error,
            "calibration_buckets": reliability_curve(confidences, correct),
            "predicted_probability_distribution": {
                label: math.fsum(row["probabilities"][label] for row in rows) / len(rows) for label in labels
            },
            "actual_outcome_frequency": {
                label: sum(row["actual_selection"] == label for row in rows) / len(rows) for label in labels
            },
        }
        betting = self._betting_metrics(rows, labels)
        return {
            "model_name": model,
            "model_version": version,
            "market": market,
            "feature_schema_version": feature,
            "config_hash": config_hash,
            "sample_size": len(rows),
            "minimum_sample": self.settings.min_sample,
            "progress": f"{len(rows)} / {self.settings.min_sample}",
            "progress_status": sample_progress(len(rows), self.settings.min_sample),
            "validation_status": assessment.status,
            "model_quality": quality,
            "betting_performance": betting,
            "competition_segments": dict(Counter(row["competition_id"] for row in rows)),
        }

    @staticmethod
    def _betting_metrics(rows, labels):
        candidates = []
        for row in rows:
            odds_values = [row["entry_odds"][label] for label in labels]
            no_vig = dict(zip(labels, devig(odds_values), strict=True))
            selection = max(labels, key=lambda label: row["probabilities"][label] - no_vig[label])
            probability = row["probabilities"][selection]
            odds = row["entry_odds"][selection]
            edge = probability - no_vig[selection]
            if edge <= 0 or probability * odds - 1 <= 0:
                continue
            won = selection == row["actual_selection"]
            closing = row["closing_odds"].get(selection)
            candidates.append(
                {
                    "profit": Decimal(str(odds - 1 if won else -1)),
                    "odds": odds,
                    "edge": edge,
                    "closing": closing,
                    "won": won,
                }
            )
        if not candidates:
            return {
                "number_of_qualifying_bets": 0,
                "roi": None,
                "yield": None,
                "clv": None,
                "max_drawdown": None,
                "hit_rate": None,
                "average_odds": None,
                "average_edge": None,
            }
        metrics = performance(
            [row["profit"] for row in candidates],
            [Decimal("1") for _ in candidates],
            Decimal(str(len(candidates))),
        )
        clv = [closing_line_value(row["odds"], row["closing"]) for row in candidates if row["closing"]]
        return {
            "number_of_qualifying_bets": len(candidates),
            "roi": metrics["roi"],
            "yield": metrics["yield"],
            "clv": math.fsum(clv) / len(clv) if clv else None,
            "max_drawdown": metrics["max_drawdown"],
            "hit_rate": sum(row["won"] for row in candidates) / len(candidates),
            "average_odds": math.fsum(row["odds"] for row in candidates) / len(candidates),
            "average_edge": math.fsum(row["edge"] for row in candidates) / len(candidates),
        }

    @staticmethod
    def _match_details(states, predictions, observations, identity_mappings, claims, results):
        details = []
        for state in states:
            match_id = state["match_id"]
            match_predictions = [row for row in predictions if row["match_id"] == match_id]
            match_observations = [row for row in observations if row["match_id"] == match_id]
            match_mapping = next(
                (
                    row
                    for row in identity_mappings
                    if row["kind"] == "match" and row["source_id"] == match_id and row.get("identity")
                ),
                None,
            )
            identity = match_mapping.get("identity", {}) if match_mapping else {}
            competition_mapping = next(
                (
                    row
                    for row in identity_mappings
                    if row["kind"] == "competition"
                    and row["source_id"] == state["competition_id"]
                    and (not identity or row["target_provider"] == identity.get("provider"))
                ),
                None,
            )
            home_mapping = next(
                (
                    row
                    for row in identity_mappings
                    if row["kind"] == "team"
                    and row["source_id"] == state["match"]["home_team_id"]
                    and (not identity or row["target_provider"] == identity.get("provider"))
                ),
                None,
            )
            away_mapping = next(
                (
                    row
                    for row in identity_mappings
                    if row["kind"] == "team"
                    and row["source_id"] == state["match"]["away_team_id"]
                    and (not identity or row["target_provider"] == identity.get("provider"))
                ),
                None,
            )
            claim = next((row for row in claims if row["match_id"] == match_id), None)
            result = next((row for row in results if row["match_id"] == match_id), None)
            details.append(
                {
                    "match_id": match_id,
                    "better_match_id": match_id,
                    "source_888_match_id": (match_id if match_id.startswith("888it:match:") else None),
                    "match": f"{state['match']['home_team']} – {state['match']['away_team']}",
                    "competition": state["competition_id"],
                    "kickoff": state["kickoff"],
                    "lifecycle_status": state["lifecycle_status"],
                    "eligibility_status": state.get("eligibility_status"),
                    "reason": state["reason"],
                    "diagnostic": state.get("details", {}).get("diagnostic"),
                    "predictions": match_predictions,
                    "observations": match_observations,
                    "provider_match_id": identity.get("provider_match_id"),
                    "competition_mapping": competition_mapping.get("target_id")
                    if competition_mapping
                    else None,
                    "home_mapping": home_mapping.get("target_id") if home_mapping else None,
                    "away_mapping": away_mapping.get("target_id") if away_mapping else None,
                    "resolution_method": identity.get("resolution_method"),
                    "resolution_confidence": identity.get("confidence"),
                    "result_provider": (claim or {}).get("provider"),
                    "result_source": (claim or {}).get("source_url"),
                    "result_source_tier": (claim or {}).get("source_tier"),
                    "result_timestamp": (claim or {}).get("retrieved_at"),
                    "regulation_score": (claim or {}).get("regulation_score"),
                    "result_status": "CONFLICT"
                    if state["lifecycle_status"] == "RESULT_CONFLICT"
                    else ("VERIFIED" if result else "PENDING"),
                    "settlement_status": "COMPLETED" if match_observations else (
                        "NOT_APPLICABLE" if state["reason"] == "PREMATCH_PREDICTION_MISSING" else "PENDING"),
                    "validation_inclusion_status": (
                        match_observations[0].get("inclusion_status") if match_observations else (
                            "VALIDATION_EXCLUDED" if state.get("eligibility_status") == "VALIDATION_EXCLUDED" else "PENDING")
                    ),
                }
            )
        return details
