"""Append-only manual betting desk built from authoritative Better analyses."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations
from uuid import NAMESPACE_URL, UUID, uuid5

from better.domain.enums import DecisionStatus, PredictionStatus
from better.domain.salabet import (
    BetCandidate,
    BetCandidateCreate,
    BetSelectionVoid,
    BetSlip,
    BetSlipAction,
    BetSlipCreate,
    BetSlipSelection,
    BetSlipUpdate,
)
from better.domain.schemas import PredictiveReport, ValidationReport, ValueReport
from better.services.bank_service import BankService, BankSettlement, BankSlipBet
from better.services.validation_cohort import ValidationCohortService
from better.storage.repository import canonical_hash


class SalaBetService:
    def __init__(self, repository, *, clock=None, settings=None):
        self.repository = repository
        self.settings = settings
        self._checkpoint_reviewer = None
        self.clock = clock or (lambda: datetime.now(UTC))
        self.bank = BankService(repository, clock=self.clock)

    def create_candidate(self, request: BetCandidateCreate) -> BetCandidate:
        return self._create_candidate(request)

    def create_indicative_candidate(self, request: BetCandidateCreate) -> BetCandidate:
        return self._create_candidate(request, indicative=True)

    def create_batch_candidate(self, request: BetCandidateCreate, batch: dict) -> BetCandidate:
        return self._create_candidate(request, batch=batch)

    def _create_candidate(self, request: BetCandidateCreate, *, batch=None, indicative=False) -> BetCandidate:
        identifier = uuid5(
            NAMESPACE_URL,
            f"salabet:candidate:{request.analysis_id}:{request.prediction_id}:"
            f"{request.market.value}:{request.selection}",
        )
        run = self.repository.get_run(request.analysis_id)
        if run is None:
            raise KeyError("analysis not found")
        if batch is None and not indicative and run.decision.status != DecisionStatus.BET:
            raise ValueError("ADD_BET_NOT_ELIGIBLE: la decisione finale di Bettor non e BET")
        if batch is None and not indicative and (run.decision.market is None or (
            run.decision.market.type != request.market
            or run.decision.market.selection != request.selection
        )):
            raise ValueError("ADD_BET_NOT_ELIGIBLE: la selezione non coincide con la decisione finale")
        if indicative:
            forecasts = [
                row for row in run.forecast.markets
                if row.method == "RAW_POISSON" and row.market == request.market
                and row.sample_size is not None and row.sample_size > 0
                and row.reason not in {"MODEL_INPUT_INSUFFICIENT", "MODEL_INPUT_FEATURES_INSUFFICIENT"}
            ]
            if not forecasts or not forecasts[0].probabilities:
                raise ValueError("MODEL_INPUT_INSUFFICIENT: nessuna previsione statistica valida")
            forecast_row = forecasts[0]
            leading = max(forecast_row.probabilities, key=forecast_row.probabilities.get)
            if request.selection != leading:
                raise ValueError("INDICATIVE_SELECTION_MISMATCH: selezione diversa dall'esito piu probabile")
            if run.snapshot.match.kickoff <= self.clock():
                raise ValueError("MATCH_ALREADY_STARTED: la partita e gia iniziata")
        existing = next(
            (item for item in self.list_candidates(include_removed=True) if item.id == identifier),
            None,
        )
        if existing is not None:
            return existing
        result = run.results.get("predictive_model")
        predictive = result.payload if result and isinstance(result.payload, PredictiveReport) else None
        if predictive is None:
            raise ValueError("MODEL_INPUT_INSUFFICIENT: no stored prediction")
        prediction = next(
            (item for item in predictive.model_predictions if item.prediction_id == request.prediction_id),
            None,
        )
        if prediction is None:
            raise KeyError("prediction not found in analysis")
        probabilities = prediction.probabilities.get(request.market)
        if probabilities is None or request.selection not in probabilities:
            raise ValueError("market or selection not present in the stored prediction")
        if indicative and abs(probabilities[request.selection] - forecast_row.probabilities[request.selection]) > 1e-8:
            raise ValueError("INDICATIVE_PREDICTION_MISMATCH: previsione non coerente con lo snapshot")

        validation_result = run.results.get("model_validator")
        validation = (
            validation_result.payload
            if validation_result and isinstance(validation_result.payload, ValidationReport)
            else None
        )
        validation_row = next(
            (
                row
                for row in (validation.validations if validation else ())
                if row.model_id == prediction.model
                and row.model_version == prediction.model_version
                and row.market == request.market
            ),
            None,
        )
        forecast = next(
            (
                item
                for item in (run.forecast.markets if run.forecast else ())
                if item.method == "RAW_POISSON" and item.market == request.market
            ),
            None,
        )
        value_result = run.results.get("value_bettor")
        value = (
            value_result.payload if value_result and isinstance(value_result.payload, ValueReport) else None
        )
        value_candidates = (*value.candidates, *value.indicative_candidates) if value is not None else ()
        comparable = [
            candidate
            for candidate in value_candidates
            if candidate.market.type == request.market
            and candidate.market.selection == request.selection
            and candidate.model_id == prediction.model
            and candidate.model_version == prediction.model_version
        ]
        priced = max(comparable, key=lambda candidate: candidate.current_odds, default=None)
        if priced is None and run.snapshot.odds:
            quotes = [
                quote
                for quote in run.snapshot.odds.quotes
                if quote.market.type == request.market
                and quote.market.selection == request.selection
                and quote.captured_at <= run.as_of
            ]
            quote = max(quotes, key=lambda item: item.odds, default=None)
        else:
            quote = None

        probability = probabilities[request.selection]
        operational = False if indicative else (
            bool(forecast.operationally_eligible) if forecast else request.market in predictive.ensemble
        )
        prediction_status = (
            PredictionStatus.INDICATIVE
            if indicative
            else forecast.prediction_status
            if forecast
            else PredictionStatus.VALIDATED
            if operational
            else prediction.prediction_status
        )
        validation_status = (
            forecast.validation_status
            if forecast
            else validation_row.validation_status
            if validation_row
            else prediction.validation_status
        )
        validation_sample = (
            forecast.validation_sample_size
            if forecast
            else validation_row.sample_size
            if validation_row
            else prediction.validation_sample_size
        )
        match = run.snapshot.match
        candidate = BetCandidate(
            id=identifier,
            analysis_id=request.analysis_id,
            prediction_id=request.prediction_id,
            match_id=run.match_id,
            competition_id=match.competition,
            kickoff=match.kickoff,
            home_team=match.home_team,
            away_team=match.away_team,
            team_ids=(match.home_team_id, match.away_team_id),
            market=request.market,
            selection=request.selection,
            probability=probability,
            fair_odds=prediction.fair_odds.get(request.market, {}).get(request.selection, 1 / probability),
            odds_at_analysis=(priced.current_odds if priced else quote.odds if quote else None),
            edge=priced.edge if priced else None,
            ev=priced.expected_value if priced else None,
            bookmaker_at_analysis=(priced.bookmaker if priced else quote.bookmaker if quote else None),
            model_name=prediction.model,
            model_version=prediction.model_version,
            prediction_status=prediction_status,
            validation_status=validation_status,
            validation_sample=validation_sample,
            validation_required=(forecast.validation_minimum if forecast else prediction.validation_minimum),
            input_completeness=prediction.input_completeness,
            operationally_eligible=operational,
            created_at=self.clock(),
            provenance={
                "analysis_as_of": run.as_of.isoformat(),
                "automation_checkpoint": batch.get("automation_checkpoint") if batch else None,
                "prediction_data_cutoff": prediction.data_cutoff.isoformat(),
                "model": f"{prediction.model}:{prediction.model_version}",
                "quote_source": (
                    priced.provenance.model_dump(mode="json")
                    if priced
                    else quote.provenance.model_dump(mode="json")
                    if quote
                    else None
                ),
            },
            batch_id=batch.get("batch_id") if batch else None,
            ranking_position=batch.get("ranking_position") if batch else None,
            ranking_score=batch.get("ranking_score") if batch else None,
            ranking_reasons=tuple(batch.get("ranking_reasons", ())) if batch else (),
            batch_status=batch.get("batch_status") if batch else None,
            batch_data_quality=batch.get("data_quality") if batch else None,
            batch_sample_size=batch.get("sample_size") if batch else None,
            ranking_breakdown=batch.get("score_breakdown", {}) if batch else {},
            batch_blockers=tuple(batch.get("blockers", ())) if batch else (),
        )
        document = candidate.model_dump(mode="json")
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "bet_candidates",
                identifier=str(identifier),
                entity_id=str(request.analysis_id),
                match_id=run.match_id,
                payload=document,
            )
            event = {
                "kind": "BET_CANDIDATE_CREATED",
                "candidate_id": str(identifier),
                "at": candidate.created_at.isoformat(),
            }
            self.repository._append(
                connection,
                "bet_events",
                identifier=f"candidate-created:{identifier}",
                entity_id=str(identifier),
                match_id=run.match_id,
                payload=event,
            )
        return candidate

    def list_candidates(self, *, include_removed=False) -> list[BetCandidate]:
        removal_state = {}
        for row in self.repository.list_records("bet_events", last=1000):
            if row.get("kind") in {"BET_CANDIDATE_REMOVED", "BET_CANDIDATE_RESTORED"}:
                sequence = row.get("visibility_sequence", 0)
                current = removal_state.get(row["candidate_id"])
                if current is None or sequence > current[0]:
                    removal_state[row["candidate_id"]] = (
                        sequence, row["kind"] == "BET_CANDIDATE_REMOVED"
                    )
        batch_metadata = {}
        for row in self.repository.list_records("annotations", last=1000):
            if row.get("event") != "BATCH_CANDIDATE" or not row.get("candidate_id"):
                continue
            batch_metadata.setdefault(row["candidate_id"], row)
        candidates = []
        for row in self.repository.list_records("bet_candidates", last=1000):
            candidate = BetCandidate.model_validate(
                {**row, "removed": removal_state.get(str(row["id"]), (0, False))[1]}
            )
            batch = batch_metadata.get(str(candidate.id))
            if batch:
                candidate = candidate.model_copy(
                    update={
                        "batch_id": UUID(batch["batch_id"]),
                        "ranking_position": batch["ranking_position"],
                        "ranking_score": batch["ranking_score"],
                        "ranking_reasons": tuple(batch.get("ranking_reasons", ())),
                        "batch_status": batch.get("batch_status"),
                        "batch_data_quality": batch.get("data_quality"),
                        "batch_sample_size": batch.get("sample_size"),
                        "ranking_breakdown": batch.get("score_breakdown", {}),
                        "batch_blockers": tuple(batch.get("blockers", ())),
                    }
                )
            if include_removed or (not candidate.removed and candidate.kickoff > self.clock()):
                candidates.append(candidate)
        return sorted(candidates, key=lambda item: (item.created_at, str(item.id)), reverse=True)

    def remove_candidate(self, candidate_id: UUID, action: BetSlipAction):
        candidate = self._candidate(candidate_id, include_removed=True)
        event = {
            "kind": "BET_CANDIDATE_REMOVED",
            "candidate_id": str(candidate_id),
            "visibility_sequence": self._candidate_visibility_sequence(candidate_id),
            "request_id": str(action.request_id),
            "at": self.clock().isoformat(),
        }
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "bet_events",
                identifier=f"salabet-event:{action.request_id}",
                entity_id=str(candidate_id),
                match_id=candidate.match_id,
                payload=event,
            )
        return {"removed": str(candidate_id)}

    def restore_candidate(self, candidate_id: UUID, action: BetSlipAction):
        candidate = self._candidate(candidate_id, include_removed=True)
        if not candidate.removed:
            return candidate
        event = {
            "kind": "BET_CANDIDATE_RESTORED",
            "candidate_id": str(candidate_id),
            "visibility_sequence": self._candidate_visibility_sequence(candidate_id),
            "request_id": str(action.request_id),
            "at": self.clock().isoformat(),
        }
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "bet_events",
                identifier=f"salabet-event:{action.request_id}",
                entity_id=str(candidate_id),
                match_id=candidate.match_id,
                payload=event,
            )
        return candidate.model_copy(update={"removed": False})

    def _candidate_visibility_sequence(self, candidate_id):
        return 1 + max(
            (
                row.get("visibility_sequence", 0)
                for row in self.repository.list_records(
                    "bet_events", last=1000, entity_id=candidate_id
                )
                if row.get("kind") in {"BET_CANDIDATE_REMOVED", "BET_CANDIDATE_RESTORED"}
            ),
            default=0,
        )

    def create_slip(self, request: BetSlipCreate) -> BetSlip:
        existing = next((item for item in self.list_slips() if item.id == request.request_id), None)
        if existing is not None:
            if tuple(item.bet_candidate_id for item in existing.selections) != request.candidate_ids:
                raise ValueError("request id already used with different candidate selections")
            return existing
        candidates = self._candidates(request.candidate_ids)
        self._validate_candidates(candidates)
        slip_type = "SINGLE" if len(candidates) == 1 else "MULTIPLE"
        created_at = self.clock()
        selections = tuple(self._selection(candidate) for candidate in candidates)
        bookmakers = [item.bookmaker_at_analysis for item in candidates if item.bookmaker_at_analysis]
        bookmaker = bookmakers[0] if bookmakers and len(set(bookmakers)) == 1 else None
        slip = BetSlip(
            id=request.request_id,
            type=slip_type,
            selections=selections,
            bookmaker=bookmaker,
            combined_odds=self._combined_odds(selections),
            warnings=self._warnings(candidates),
            created_at=created_at,
        )
        document = slip.model_dump(mode="json")
        event = self._event("BETSLIP_CREATED", request.request_id, slip, sequence=1)
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "bet_slips",
                identifier=str(slip.id),
                entity_id=str(slip.id),
                match_id=candidates[0].match_id if slip_type == "SINGLE" else None,
                payload=document,
            )
            self._append_selections(connection, slip)
            self.repository._append(
                connection,
                "bet_events",
                identifier=f"salabet-event:{request.request_id}",
                entity_id=str(slip.id),
                match_id=candidates[0].match_id if slip_type == "SINGLE" else None,
                payload=event,
            )
        return slip

    def update_slip(self, slip_id: UUID, update: BetSlipUpdate) -> BetSlip:
        current = self.get_slip(slip_id)
        if current.status not in {"DRAFT", "READY"}:
            raise ValueError("post-place economic fields are immutable")
        candidates = (
            self._candidates(update.candidate_ids)
            if update.candidate_ids is not None
            else [self._candidate(selection.bet_candidate_id) for selection in current.selections]
        )
        self._validate_candidates(candidates)
        previous = {selection.bet_candidate_id: selection for selection in current.selections}
        odds = update.odds_at_placement or {}
        unknown = set(odds) - {candidate.id for candidate in candidates}
        if unknown:
            raise ValueError("odds supplied for a selection outside the slip")
        selections = []
        for candidate in candidates:
            base = self._selection(candidate)
            old = previous.get(candidate.id)
            selections.append(
                base.model_copy(
                    update={
                        "odds_at_placement": odds.get(candidate.id, old.odds_at_placement if old else None)
                    }
                )
            )
        stake = update.stake if update.stake is not None else current.stake
        bookmaker = update.bookmaker if update.bookmaker is not None else current.bookmaker
        note = update.note if update.note is not None else current.note
        combined = self._combined_odds(selections)
        potential = self._potential_return(stake, combined)
        updated = BetSlip(
            id=current.id,
            type="SINGLE" if len(selections) == 1 else "MULTIPLE",
            selections=tuple(selections),
            bookmaker=bookmaker,
            combined_odds=combined,
            stake=stake,
            potential_return=potential,
            status="DRAFT",
            warnings=self._warnings(candidates),
            note=note,
            created_at=current.created_at,
        )
        self._append_slip_event(updated, "BETSLIP_UPDATED", update.request_id)
        if update.note is not None:
            with self.repository.engine.begin() as connection:
                self.repository._append(
                    connection,
                    "bet_notes",
                    identifier=f"salabet-note:{update.request_id}",
                    entity_id=str(slip_id),
                    match_id=None,
                    payload={
                        "slip_id": str(slip_id),
                        "note": update.note,
                        "at": self.clock().isoformat(),
                    },
                )
        return updated

    def confirm_slip(self, slip_id: UUID, action: BetSlipAction) -> BetSlip:
        slip = self.get_slip(slip_id)
        if slip.status not in {"DRAFT", "READY"}:
            raise ValueError("only draft slips can be prepared")
        self._validate_ready(slip)
        confirmed = slip.model_copy(
            update={"status": "READY", "confirmed_at": slip.confirmed_at or self.clock()}
        )
        self._append_slip_event(confirmed, "BETSLIP_CONFIRMED", action.request_id)
        return confirmed

    def place_slip(self, slip_id: UUID, action: BetSlipAction) -> BetSlip:
        slip = self.get_slip(slip_id)
        if slip.status == "OPEN":
            return slip
        if slip.status != "READY":
            raise ValueError("SLIP_NOT_READY: prepara la schedina prima di confermare la puntata")
        self._validate_ready(slip)
        self._validate_bank_for_place(slip)
        assert slip.stake is not None and slip.combined_odds is not None and slip.bookmaker
        kickoff = min(selection.kickoff for selection in slip.selections)
        team_ids = tuple(dict.fromkeys(team for item in slip.selections for team in item.team_ids))
        competition = (
            slip.selections[0].competition_id
            if len({item.competition_id for item in slip.selections}) == 1
            else "MULTIPLE"
        )
        self.bank.record_slip(
            BankSlipBet(
                request_id=slip.id,
                betslip_id=slip.id,
                amount=slip.stake,
                odds=slip.combined_odds,
                bookmaker=slip.bookmaker,
                kickoff=kickoff,
                match_id=(slip.selections[0].match_id if slip.type == "SINGLE" else f"salabet:{slip.id}"),
                team_ids=team_ids,
                competition=competition,
                market=self._market(slip.selections[0]),
            )
        )
        placed = slip.model_copy(update={"status": "OPEN", "placed_at": self.clock()})
        self._append_slip_event(placed, "BET_PLACED", action.request_id)
        return placed

    def cancel_slip(self, slip_id: UUID, action: BetSlipAction) -> BetSlip:
        slip = self.get_slip(slip_id)
        if slip.status not in {"DRAFT", "READY"}:
            raise ValueError("a placed slip cannot be cancelled or deleted")
        cancelled = slip.model_copy(update={"status": "CANCELLED"})
        self._append_slip_event(cancelled, "BETSLIP_CANCELLED", action.request_id)
        return cancelled

    def void_selection(self, slip_id: UUID, candidate_id: UUID, request: BetSelectionVoid) -> BetSlip:
        return self._settle_selection(
            slip_id,
            candidate_id,
            "VOID",
            request.request_id,
            {"source": "MANUAL_VOID", "note": request.note},
        )

    def settle_verified(self):
        results = {
            row["match_id"]: row
            for row in reversed(self.repository.list_records("match_results", last=1000))
            if row.get("home_score") is not None
            and row.get("away_score") is not None
            and row.get("status") == "FINAL"
            and row.get("validation_eligible") is True
            and row.get("source_tier") in {"A", "B"}
        }
        settled = []
        for slip in self.list_slips():
            if slip.status != "OPEN":
                continue
            for selection in slip.selections:
                result = results.get(selection.match_id)
                if selection.result != "UNRESOLVED" or result is None:
                    continue
                actual = ValidationCohortService.actual_selection(
                    selection.market, result["home_score"], result["away_score"]
                )
                outcome = "WON" if selection.selection == actual else "LOST"
                request_id = uuid5(
                    NAMESPACE_URL,
                    f"salabet:settlement:{slip.id}:{selection.bet_candidate_id}:"
                    f"{result.get('result_id', canonical_hash(result))}",
                )
                current = self._settle_selection(
                    slip.id,
                    selection.bet_candidate_id,
                    outcome,
                    request_id,
                    {
                        "source": "VERIFIED_MATCH_RESULT",
                        "result_provider": result.get("provider"),
                        "result_source": result.get("source_url"),
                        "regulation_score": [result["home_score"], result["away_score"]],
                    },
                )
                settled.append(
                    {
                        "slip_id": str(slip.id),
                        "candidate_id": str(selection.bet_candidate_id),
                        "result": outcome,
                        "slip_status": current.status,
                    }
                )
        return settled

    def list_slips(self) -> list[BetSlip]:
        base = {
            row["id"]: BetSlip.model_validate(row)
            for row in self.repository.list_records("bet_slips", last=1000)
        }
        events = defaultdict(list)
        for event in self.repository.list_records("bet_events", last=1000):
            if event.get("slip_id") in base and event.get("state"):
                events[event["slip_id"]].append(event)
        projected = []
        for identifier, initial in base.items():
            latest = max(events[identifier], key=lambda item: item["sequence"], default=None)
            projected.append(BetSlip.model_validate(latest["state"]) if latest else initial)
        return sorted(projected, key=lambda item: (item.created_at, str(item.id)), reverse=True)

    def list_visible_slips(self) -> list[BetSlip]:
        hidden = self._hidden_slips()
        return [slip for slip in self.list_slips() if not hidden.get(str(slip.id), False)]

    def get_slip(self, slip_id: UUID) -> BetSlip:
        slip = next((item for item in self.list_slips() if item.id == slip_id), None)
        if slip is None:
            raise KeyError("slip not found")
        return slip

    def archive(self):
        return self.archive_records()

    def archive_records(self, *, include_hidden=False):
        bank = self.bank.status()
        bank_bets = {row["request_id"]: row for row in bank["bets"]}
        hidden = self._hidden_slips()
        rows = []
        for slip in self.list_slips():
            is_hidden = hidden.get(str(slip.id), False)
            if is_hidden and not include_hidden:
                continue
            economic = bank_bets.get(str(slip.id))
            stake = Decimal(economic["amount"]) if economic else None
            returned = (
                Decimal(economic["return"]) if economic and economic.get("return") is not None else None
            )
            rows.append(
                {
                    **slip.model_dump(mode="json"),
                    "bank_stake": str(stake) if stake is not None else None,
                    "bank_return": str(returned) if returned is not None else None,
                    "profit_loss": (
                        str(returned - stake) if returned is not None and stake is not None else None
                    ),
                    "bank_status": economic.get("status") if economic else None,
                    "hidden": is_hidden,
                    "selections": [
                        {
                            **selection.model_dump(mode="json"),
                            "match_status": self._match_status(selection.match_id, selection.kickoff),
                        }
                        for selection in slip.selections
                    ],
                }
            )
        return rows

    def hide_slip(self, slip_id: UUID, action: BetSlipAction):
        slip = self.get_slip(slip_id)
        self._append_slip_event(slip, "HIDDEN", action.request_id)
        return {"slip_id": str(slip_id), "hidden": True}

    def restore_slip(self, slip_id: UUID, action: BetSlipAction):
        slip = self.get_slip(slip_id)
        self._append_slip_event(slip, "RESTORED", action.request_id)
        return {"slip_id": str(slip_id), "hidden": False}

    def slip_view(self, slip: BetSlip):
        document = slip.model_dump(mode="json")
        selections = []
        for selection in slip.selections:
            verified = self._verified_result(selection.match_id)
            score = (
                {
                    "home": verified["home_score"],
                    "away": verified["away_score"],
                    "source": verified.get("provider") or verified.get("source"),
                    "source_tier": verified["source_tier"],
                }
                if verified is not None
                else None
            )
            selections.append({
                **selection.model_dump(mode="json"),
                "match_status": (
                    "CONCLUSA" if score is not None
                    else self._match_status(selection.match_id, selection.kickoff)
                ),
                "final_score": score,
            })
        document["selections"] = selections
        automatic = self.repository.all_records("annotations", entity_id="auto-multiple-slip:" + str(slip.id))
        document["automatic_preparation"] = automatic[0] if automatic else None
        scheduled = [r for r in self.repository.all_records("annotations", entity_id="automatic-multiples:v1")
                     if r.get("slip_id") == str(slip.id) and r.get("kind") in {"SCHEDULED", "SCHEDULE_FINISHED"}]
        document["confirmation_schedule"] = {
            **next((r for r in scheduled if r['kind'] == 'SCHEDULED'), {}),
            **next((r for r in scheduled if r['kind'] == 'SCHEDULE_FINISHED'), {}),
        } or None
        document["checkpoint_review"] = None
        return document

    def _verified_result(self, match_id):
        for row in self.repository.list_records("match_results", last=10, entity_id=match_id):
            if (
                row.get("status") == "FINAL"
                and row.get("validation_eligible") is True
                and row.get("source_tier") in {"A", "B"}
                and type(row.get("home_score")) is int
                and type(row.get("away_score")) is int
            ):
                return row
        return None

    def stats(self):
        archive = self.archive_records(include_hidden=True)
        bank = self.bank.status()
        placed = [row for row in archive if row["bank_stake"] is not None]
        closed = [row for row in placed if row["bank_return"] is not None]
        stake = sum((Decimal(row["bank_stake"]) for row in placed), Decimal(0))
        returned = sum((Decimal(row["bank_return"]) for row in closed), Decimal(0))
        closed_stake = sum((Decimal(row["bank_stake"]) for row in closed), Decimal(0))
        profit = returned - closed_stake
        initial = Decimal(bank["initial_budget"])

        def group(key):
            grouped = defaultdict(Decimal)
            for row in closed:
                grouped[key(row)] += Decimal(row["profit_loss"])
            return {name: str(value) for name, value in sorted(grouped.items())}

        resolved = [row for row in closed if row["status"] != "VOID"]
        return {
            "total_bets": len(placed),
            "singles": sum(row["type"] == "SINGLE" for row in placed),
            "multiples": sum(row["type"] == "MULTIPLE" for row in placed),
            "open": sum(row["status"] == "OPEN" for row in placed),
            "stake_total": str(stake),
            "return_total": str(returned),
            "profit_loss": str(profit),
            "roi": float(profit / initial) if initial else 0,
            "yield": float(profit / closed_stake) if closed_stake else 0,
            "hit_rate": (sum(row["status"] == "WON" for row in resolved) / len(resolved) if resolved else 0),
            "average_odds": (
                sum(Decimal(str(row["combined_odds"])) for row in placed) / len(placed)
                if placed
                else Decimal(0)
            ),
            "profit_by_market": group(
                lambda row: row["selections"][0]["market"] if row["type"] == "SINGLE" else "MULTIPLE"
            ),
            "profit_by_competition": group(
                lambda row: (
                    row["selections"][0]["competition_id"]
                    if len({item["competition_id"] for item in row["selections"]}) == 1
                    else "MULTIPLE"
                )
            ),
            "profit_by_bookmaker": group(lambda row: row["bookmaker"] or "UNKNOWN"),
            "bankroll": bank,
            "bankroll_history": self.bank.timeline(),
            "model_quality_separate": True,
        }

    def _settle_selection(self, slip_id, candidate_id, result, request_id, evidence):
        slip = self.get_slip(slip_id)
        if slip.status != "OPEN":
            if slip.status in {"WON", "LOST", "VOID", "PARTIAL_VOID"}:
                return slip
            raise ValueError("only an open placed slip can be settled")
        if result not in {"WON", "LOST", "VOID"}:
            raise ValueError("invalid selection result")
        found = False
        selections = []
        for selection in slip.selections:
            if selection.bet_candidate_id == candidate_id:
                found = True
                if selection.result != "UNRESOLVED":
                    return slip
                selection = selection.model_copy(update={"result": result, "settlement_status": "SETTLED"})
            selections.append(selection)
        if not found:
            raise KeyError("selection not found in slip")
        unresolved = any(item.result == "UNRESOLVED" for item in selections)
        if unresolved:
            status = "OPEN"
        elif any(item.result == "LOST" for item in selections):
            status = "LOST"
        elif all(item.result == "VOID" for item in selections):
            status = "VOID"
        elif any(item.result == "VOID" for item in selections):
            status = "PARTIAL_VOID"
        else:
            status = "WON"
        settled_at = self.clock() if status != "OPEN" else None
        updated = slip.model_copy(
            update={"selections": tuple(selections), "status": status, "settled_at": settled_at}
        )
        if status != "OPEN":
            assert slip.stake is not None
            effective_odds = Decimal(1)
            for item in selections:
                if item.result == "WON":
                    effective_odds *= Decimal(str(item.odds_at_placement))
            returned = (
                Decimal(0)
                if status == "LOST"
                else slip.stake
                if status == "VOID"
                else (slip.stake * effective_odds).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
            bank_result = "LOST" if status == "LOST" else "VOID" if status == "VOID" else "WON"
            bank_request = BankSettlement(
                request_id=uuid5(NAMESPACE_URL, f"salabet:bank-settlement:{slip.id}"),
                result=bank_result,
            )
            # Banca is the economic authority. Its idempotent ledger write must succeed
            # before SalaBet projects the terminal state, otherwise a retry could see a
            # closed selection without the matching movement.
            self.bank.settle_slip(slip.id, bank_request, returned)
        if status != "OPEN":
            final_request = uuid5(NAMESPACE_URL, f"salabet:slip-settled:{slip.id}")
            self._append_terminal_settlement(
                updated,
                request_id,
                {"candidate_id": str(candidate_id), "result": result, **evidence},
                "BETSLIP_SETTLED" if status != "VOID" else "BETSLIP_VOIDED",
                final_request,
                {
                    "return": str(returned),
                    "effective_odds": str(effective_odds),
                    "void_leg_policy": "VOID_ODDS_1_00",
                },
            )
        else:
            self._append_slip_event(
                updated,
                "SELECTION_SETTLED",
                request_id,
                details={"candidate_id": str(candidate_id), "result": result, **evidence},
            )
        return updated

    def _validate_ready(self, slip):
        if not slip.bookmaker or slip.stake is None:
            raise ValueError("bookmaker and stake are required")
        if any(item.odds_at_placement is None or item.odds_at_placement <= 1 for item in slip.selections):
            raise ValueError("INVALID_ODDS: ogni selezione richiede una quota maggiore di 1")
        if self.clock() >= min(item.kickoff for item in slip.selections):
            raise ValueError(
                "MATCH_ALREADY_STARTED: la partita è già iniziata; il live betting non è supportato"
            )

    def _validate_bank_for_place(self, slip):
        state = self.bank.status()
        if not state["initialized"]:
            raise ValueError("BANK_NOT_INITIALIZED: inizializza la Banca prima del place")
        if slip.stake > Decimal(state["available"]):
            raise ValueError("INSUFFICIENT_BANKROLL: bankroll disponibile insufficiente")

    def _append_slip_event(self, slip, kind, request_id, details=None):
        existing = next(
            (
                row
                for row in self.repository.list_records("bet_events", last=1000)
                if row.get("request_id") == str(request_id) and row.get("slip_id") == str(slip.id)
            ),
            None,
        )
        if existing:
            if existing["kind"] != kind or existing.get("state") != slip.model_dump(mode="json"):
                raise ValueError("request id already used with different slip contents")
            return
        sequence = 1 + max(
            (
                row.get("sequence", 0)
                for row in self.repository.list_records("bet_events", last=1000, entity_id=slip.id)
                if row.get("slip_id") == str(slip.id)
            ),
            default=0,
        )
        event = self._event(kind, request_id, slip, sequence=sequence, details=details)
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "bet_events",
                identifier=f"salabet-event:{request_id}",
                entity_id=str(slip.id),
                match_id=slip.selections[0].match_id if slip.type == "SINGLE" else None,
                payload=event,
            )
            self._append_selections(connection, slip)

    def _append_terminal_settlement(
        self,
        slip,
        selection_request_id,
        selection_details,
        final_kind,
        final_request_id,
        final_details,
    ):
        rows = [
            row
            for row in self.repository.list_records("bet_events", last=1000, entity_id=slip.id)
            if row.get("slip_id") == str(slip.id)
        ]
        specifications = (
            ("SELECTION_SETTLED", selection_request_id, selection_details),
            (final_kind, final_request_id, final_details),
        )
        missing = []
        state = slip.model_dump(mode="json")
        for kind, request_id, details in specifications:
            existing = next((row for row in rows if row.get("request_id") == str(request_id)), None)
            if existing is not None:
                if existing["kind"] != kind or existing.get("state") != state:
                    raise ValueError("request id already used with different slip contents")
            else:
                missing.append((kind, request_id, details))
        if not missing:
            return
        sequence = 1 + max((row.get("sequence", 0) for row in rows), default=0)
        with self.repository.engine.begin() as connection:
            for kind, request_id, details in missing:
                event = self._event(kind, request_id, slip, sequence=sequence, details=details)
                self.repository._append(
                    connection,
                    "bet_events",
                    identifier=f"salabet-event:{request_id}",
                    entity_id=str(slip.id),
                    match_id=slip.selections[0].match_id if slip.type == "SINGLE" else None,
                    payload=event,
                )
                sequence += 1
            self._append_selections(connection, slip)

    def _event(self, kind, request_id, slip, *, sequence, details=None):
        return {
            "kind": kind,
            "request_id": str(request_id),
            "slip_id": str(slip.id),
            "sequence": sequence,
            "at": self.clock().isoformat(),
            "details": details or {},
            "state": slip.model_dump(mode="json"),
        }

    def _append_selections(self, connection, slip):
        for selection in slip.selections:
            document = selection.model_dump(mode="json")
            self.repository._append(
                connection,
                "bet_slip_selections",
                identifier=f"{slip.id}:{selection.bet_candidate_id}:{canonical_hash(document)}",
                entity_id=str(slip.id),
                match_id=selection.match_id,
                payload={"slip_id": str(slip.id), **document},
            )

    def _candidate(self, identifier, *, include_removed=False):
        candidate = next(
            (item for item in self.list_candidates(include_removed=include_removed) if item.id == identifier),
            None,
        )
        if candidate is None:
            raise KeyError("candidate not found")
        if candidate.removed and not include_removed:
            raise ValueError("candidate was removed")
        return candidate

    def _candidates(self, identifiers):
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate candidate identifiers are forbidden")
        return [self._candidate(identifier) for identifier in identifiers]

    @staticmethod
    def _validate_candidates(candidates):
        if not 1 <= len(candidates) <= 6:
            raise ValueError("a slip requires one to six selections")
        semantic = {(item.match_id, item.market, item.selection) for item in candidates}
        if len(semantic) != len(candidates):
            raise ValueError("duplicate semantic selections are forbidden")
        for one, two in combinations(candidates, 2):
            if one.match_id == two.match_id and one.market == two.market and one.selection != two.selection:
                raise ValueError("mutually exclusive selections cannot share a slip")

    @staticmethod
    def _warnings(candidates):
        correlated = any(
            one.match_id == two.match_id and one.market != two.market
            for one, two in combinations(candidates, 2)
        )
        return ("CORRELATED_SELECTIONS_WARNING",) if correlated else ()

    @staticmethod
    def _selection(candidate):
        return BetSlipSelection(
            bet_candidate_id=candidate.id,
            analysis_id=candidate.analysis_id,
            prediction_id=candidate.prediction_id,
            match_id=candidate.match_id,
            competition_id=candidate.competition_id,
            kickoff=candidate.kickoff,
            home_team=candidate.home_team,
            away_team=candidate.away_team,
            team_ids=candidate.team_ids,
            market=candidate.market,
            selection=candidate.selection,
            odds_at_analysis=candidate.odds_at_analysis,
            odds_at_placement=candidate.odds_at_analysis,
            probability=candidate.probability,
            fair_odds=candidate.fair_odds,
            edge=candidate.edge,
            validation_status=candidate.validation_status,
            prediction_status=candidate.prediction_status,
            operationally_eligible=candidate.operationally_eligible,
        )

    @staticmethod
    def _combined_odds(selections):
        if not selections or any(item.odds_at_placement is None for item in selections):
            return None
        value = Decimal(1)
        for selection in selections:
            value *= Decimal(str(selection.odds_at_placement))
        return float(value)

    @staticmethod
    def _potential_return(stake, combined):
        if stake is None or combined is None:
            return None
        return (stake * Decimal(str(combined))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _market(selection):
        from better.domain.models import Market

        return Market(type=selection.market, selection=selection.selection)

    def _hidden_slips(self):
        states = {}
        rows = sorted(
            self.repository.list_records("bet_events", last=1000),
            key=lambda row: (row.get("at", ""), row.get("sequence", 0)),
        )
        for row in rows:
            if row.get("kind") == "HIDDEN":
                states[row["slip_id"]] = True
            elif row.get("kind") == "RESTORED":
                states[row["slip_id"]] = False
        return states

    def _match_status(self, match_id, kickoff):
        mapping = {
            "SCHEDULED": "NON_INIZIATA",
            "TIMED": "NON_INIZIATA",
            "NS": "NON_INIZIATA",
            "TBD": "NON_INIZIATA",
            "IN_PLAY": "IN_CORSO",
            "LIVE": "IN_CORSO",
            "1H": "IN_CORSO",
            "2H": "IN_CORSO",
            "ET": "IN_CORSO",
            "PAUSED": "INTERVALLO",
            "HT": "INTERVALLO",
            "FINAL": "CONCLUSA",
            "FINISHED": "CONCLUSA",
            "FT": "CONCLUSA",
            "AET": "CONCLUSA",
            "PEN": "CONCLUSA",
            "POSTPONED": "RINVIATA",
            "PST": "RINVIATA",
            "SUSPENDED": "SOSPESA",
            "SUSP": "SOSPESA",
            "CANCELLED": "ANNULLATA",
            "CANC": "ANNULLATA",
        }
        if self._verified_result(match_id) is not None:
            return "CONCLUSA"
        sources = (
            self.repository.list_records("match_result_claims", last=10, entity_id=match_id),
            self.repository.list_records("validation_cohort", last=20, entity_id=match_id),
        )
        for rows in sources:
            for row in rows:
                for key in ("provider_status", "status", "lifecycle_status"):
                    normalized = str(row.get(key, "")).upper()
                    if normalized in {"FINAL", "FINISHED", "FT", "AET", "PEN"}:
                        continue
                    if normalized in mapping:
                        return mapping[normalized]
        now = self.clock()
        if now < kickoff:
            return "NON_INIZIATA"
        return "RISULTATO_IN_ATTESA" if now >= kickoff + timedelta(hours=3) else "IN_CORSO"
