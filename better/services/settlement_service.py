"""Post-match result ingestion and cutoff-safe forecast settlement."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import Field, HttpUrl

from better.domain.common import Contract
from better.domain.enums import AnalysisMode, MarketType
from better.domain.models import HistoricalPrediction
from better.services.data_pipeline import checkpoint_for
from better.storage.repository import canonical_hash


class MatchResultInput(Contract):
    match_id: str = Field(min_length=1)
    home_score: int = Field(ge=0, le=50)
    away_score: int = Field(ge=0, le=50)
    source_url: HttpUrl
    publisher: str = Field(min_length=2, max_length=200)
    source_tier: Literal["A", "B"]
    extra_time_home: int | None = Field(default=None, ge=0, le=50)
    extra_time_away: int | None = Field(default=None, ge=0, le=50)
    penalties_home: int | None = Field(default=None, ge=0, le=50)
    penalties_away: int | None = Field(default=None, ge=0, le=50)
    published_at: datetime | None = None
    provider: str = "manual"


class SettlementService:
    def __init__(self, repository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def pending(self) -> list[dict]:
        now = self.clock()
        settled = {row["match_id"] for row in self.repository.list_records("match_results", last=1000)}
        pending = []
        seen = set()
        for run in reversed(self.repository.list_records("analysis_runs", last=1000)):
            match = run["snapshot"]["match"]
            match_id = match["match_id"]
            kickoff = datetime.fromisoformat(match["kickoff"])
            if match_id in seen or match_id in settled or kickoff + timedelta(minutes=90) > now:
                continue
            seen.add(match_id)
            pending.append(
                {
                    "match_id": match_id,
                    "match": f"{match['home_team']} – {match['away_team']}",
                    "competition": match["competition"],
                    "kickoff": match["kickoff"],
                }
            )
        return pending

    def record(self, request: MatchResultInput) -> dict:
        now = self.clock()
        match = self.repository.get_match_record(request.match_id)
        if match is None:
            raise KeyError("match not found")
        if now < match.kickoff + timedelta(minutes=90):
            raise ValueError("Final result cannot be recorded before regulation time has elapsed")
        existing = self.repository.list_records("match_results", last=1000, entity_id=match.match_id)
        identity = {
            "match_id": match.match_id,
            "home_score": request.home_score,
            "away_score": request.away_score,
            "source_url": str(request.source_url),
            "publisher": request.publisher,
            "source_tier": request.source_tier,
        }
        if existing:
            if (existing[0]["home_score"], existing[0]["away_score"]) != (
                request.home_score,
                request.away_score,
            ):
                raise ValueError("A different immutable result already exists for this match")
            canonical = self._settle_canonical(match, request, now)
            return {
                **existing[0],
                "cached": True,
                "settled_predictions": 0,
                "canonical_observations": len(canonical),
            }
        document = {
            **identity,
            "status": "FINAL",
            "recorded_at": now.isoformat(),
            "source_type": "MANUAL_TRANSCRIPTION",
            "validation_eligible": True,
        }
        closing = self._closing_quotes(match.match_id, match.kickoff)
        predictions = self._settle_forecasts(match, now, request, closing)
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "match_results",
                identifier=f"match-result:{match.match_id}",
                entity_id=match.match_id,
                match_id=match.match_id,
                payload=document,
            )
            for key, quote in closing.items():
                payload = {
                    "match_id": match.match_id,
                    "market": key[0],
                    "selection": key[1],
                    "bookmaker": key[2],
                    "closing_odds": quote["odds"],
                    "captured_at": quote["captured_at"],
                    "source": quote.get("provenance"),
                }
                self.repository._append(
                    connection,
                    "closing_prices",
                    identifier=canonical_hash(payload),
                    entity_id=f"{match.match_id}:{key[0]}:{key[1]}:{key[2]}",
                    match_id=match.match_id,
                    payload=payload,
                )
            for prediction in predictions:
                payload = prediction.model_dump(mode="json")
                self.repository._append(
                    connection,
                    "settled_predictions",
                    identifier=canonical_hash(payload),
                    entity_id=f"{prediction.model_id}:{prediction.market}:{prediction.selection}",
                    match_id=match.match_id,
                    payload=payload,
                )
        canonical = self._settle_canonical(match, request, now)
        return {
            **document,
            "cached": False,
            "closing_prices": len(closing),
            "settled_predictions": len(predictions),
            "canonical_observations": len(canonical),
        }

    def _settle_canonical(self, match, request, now):
        from better.config import Settings
        from better.providers.results import MatchResult
        from better.services.validation_cohort import ValidationCohortService

        result = MatchResult(
            match_id=match.match_id,
            final_status="FINAL",
            home_score=request.home_score,
            away_score=request.away_score,
            extra_time_home=request.extra_time_home,
            extra_time_away=request.extra_time_away,
            penalties_home=request.penalties_home,
            penalties_away=request.penalties_away,
            source=request.publisher,
            source_tier=request.source_tier,
            source_url=request.source_url,
            published_at=request.published_at,
            retrieved_at=now,
            provider=request.provider,
            verified_at=now,
        )
        return ValidationCohortService(
            repository=self.repository,
            settings=Settings(),
            clock=self.clock,
        ).record_result(match, result, now)

    def _closing_quotes(self, match_id, kickoff):
        latest = {}
        observations = self.repository.list_records("historical_observations", last=1000, entity_id=match_id)
        for observation in observations:
            odds = observation.get("snapshots", {}).get("odds") or {}
            for quote in odds.get("quotes", []):
                captured = datetime.fromisoformat(quote["captured_at"])
                if captured >= kickoff:
                    continue
                market = quote["market"]
                key = (market["type"], market["selection"], quote["bookmaker"])
                old = latest.get(key)
                if old is None or captured > datetime.fromisoformat(old["captured_at"]):
                    latest[key] = quote
        return latest

    def _settle_forecasts(self, match, settled_at, request, closing):
        records = []
        seen = set()
        for run in self.repository.analysis_runs_for_match(match.match_id):
            if run.as_of >= match.kickoff or run.forecast is None:
                continue
            checkpoint = checkpoint_for(
                match.kickoff,
                run.as_of,
                official_lineup=run.mode == AnalysisMode.POST_LINEUP,
            )
            entry_quotes = {}
            if run.snapshot.odds:
                for quote in run.snapshot.odds.quotes:
                    entry_quotes[(quote.market.type.value, quote.market.selection, quote.bookmaker)] = quote
            for forecast in run.forecast.markets:
                if forecast.method == "RAW_POISSON":
                    model, _, version = forecast.source.partition(":")
                    version = version or "unknown"
                else:
                    model, version = f"market_no_vig:{forecast.source}", "1.0.0"
                model_id = f"{model}@{checkpoint}"
                for selection, probability in forecast.probabilities.items():
                    identity = (model_id, version, forecast.market, selection, match.match_id)
                    if identity in seen:
                        continue
                    quote = next(
                        (
                            value
                            for (market, outcome, _bookmaker), value in entry_quotes.items()
                            if market == forecast.market.value and outcome == selection
                        ),
                        None,
                    )
                    if quote is None:
                        continue
                    closing_quote = closing.get((forecast.market.value, selection, quote.bookmaker))
                    outcome = self._outcome(
                        forecast.market, selection, request.home_score, request.away_score
                    )
                    profit = Decimal(str(quote.odds - 1)) if outcome else Decimal("-1")
                    records.append(
                        HistoricalPrediction(
                            match_id=match.match_id,
                            model_id=model_id,
                            model_version=version,
                            competition=match.competition,
                            market=forecast.market,
                            selection=selection,
                            probability=probability,
                            outcome=outcome,
                            predicted_at=run.as_of,
                            settled_at=settled_at,
                            data_cutoff=run.as_of,
                            split="LIVE",
                            odds=quote.odds,
                            closing_odds=closing_quote["odds"] if closing_quote else None,
                            stake=Decimal("1"),
                            profit=profit,
                            season=match.season,
                            bookmaker=quote.bookmaker,
                        )
                    )
                    seen.add(identity)
        return records

    @staticmethod
    def _outcome(market: MarketType, selection: str, home: int, away: int) -> int:
        if market == MarketType.MATCH_WINNER:
            winner = "HOME" if home > away else "AWAY" if away > home else "DRAW"
            return int(selection == winner)
        if market == MarketType.OVER_UNDER_2_5:
            result = "OVER_2_5" if home + away > 2.5 else "UNDER_2_5"
            return int(selection == result)
        if market == MarketType.BTTS:
            result = "YES" if home > 0 and away > 0 else "NO"
            return int(selection == result)
        raise ValueError("Unsupported settlement market")
