from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from better.config import Settings
from better.domain.enums import MarketType
from better.domain.models import Match
from better.providers.fake import make_demo_bundle
from better.providers.results import CompositeResultProvider, FootballDataOrgResultProvider, MatchResult
from better.services.application import make_application
from better.services.data_pipeline import DataPipelineCollector
from better.services.validation_cohort import ValidationCohortService, sample_progress
from better.storage.repository import Repository, canonical_hash
from better.system_one import SystemOneSettings

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


class ResultProvider:
    def __init__(self, home=2, away=1, status="FINAL"):
        self.home = home
        self.away = away
        self.status = status
        self.calls = 0

    async def get_result(self, match, as_of):
        self.calls += 1
        if self.status == "NONE":
            return None
        return MatchResult(
            match_id=match.match_id,
            final_status=self.status,
            home_score=self.home if self.status == "FINAL" else None,
            away_score=self.away if self.status == "FINAL" else None,
            source="Official competition",
            source_tier="A",
            source_url="https://competition.example/match/demo-match",
            published_at=as_of,
            retrieved_at=as_of,
            provider="official-test",
            verified_at=as_of,
        )


def settings(**changes):
    return Settings(
        validation_verified_competitions=("SYNTHETIC_LEAGUE",),
        **changes,
    )


def runtime(*, now=NOW, result_provider=None, configured=None):
    repository = Repository("sqlite:///:memory:")
    bundle = make_demo_bundle("bet", as_of=now)
    safe_time = now - timedelta(days=1)
    safe_quotes = tuple(
        quote.model_copy(
            update={
                "captured_at": safe_time,
                "provenance": quote.provenance.model_copy(
                    update={"retrieved_at": safe_time, "published_at": safe_time}
                ),
            }
        )
        for quote in bundle.odds.odds.quotes
    )
    bundle.odds.odds = bundle.odds.odds.model_copy(update={"quotes": safe_quotes})
    bundle = replace(bundle, results=result_provider)
    app = make_application(
        settings=configured or settings(),
        repository=repository,
        providers=bundle,
        system_one_settings=SystemOneSettings(enabled=False),
    )
    return app, repository


@pytest.mark.parametrize(
    ("size", "label"),
    [
        (0, "RACCOLTA INIZIALE"),
        (50, "CAMPIONE PRELIMINARE"),
        (100, "VALIDAZIONE IN CORSO"),
        (199, "VALIDAZIONE IN CORSO"),
        (200, "CAMPIONE MINIMO RAGGIUNTO"),
    ],
)
def test_sample_progress_does_not_change_the_200_threshold(size, label):
    assert sample_progress(size) == label


def test_enrollment_accepts_only_explicitly_verified_competition():
    app, repository = runtime()
    service = ValidationCohortService(app, clock=lambda: NOW)
    accepted = service.enroll(app.providers.fixtures.match)
    rejected_match = app.providers.fixtures.match.model_copy(update={"competition": "UNVERIFIED"})
    rejected = service.enroll(rejected_match)
    assert accepted["eligibility_status"] == "VALIDATION_ELIGIBLE"
    assert rejected["eligibility_status"] == "VALIDATION_EXCLUDED"
    assert rejected["reason"] == "COMPETITION_NOT_VERIFIED"
    assert repository.get_match_record("demo-match") is not None


def test_started_match_is_excluded_from_enrollment():
    app, _ = runtime()
    service = ValidationCohortService(app, clock=lambda: NOW + timedelta(hours=3))
    row = service.enroll(app.providers.fixtures.match)
    assert row["reason"] == "MATCH_ALREADY_STARTED"


async def test_auto_enrollment_is_independent_from_manual_selection():
    app, repository = runtime()
    service = ValidationCohortService(app, clock=lambda: NOW)
    summary = await service.auto_enroll()
    assert summary == {"discovered": 1, "enrolled": 1, "excluded": 0}
    assert repository.active_collection_selections(NOW) == []
    assert service.active_pre_match(NOW)[0]["match_id"] == "demo-match"


async def test_collector_creates_one_canonical_prediction_per_market_and_is_idempotent():
    app, repository = runtime()
    collector = DataPipelineCollector(app, clock=lambda: NOW)
    first = await collector.collect_due()
    second = await collector.collect_due()
    predictions = repository.list_records("canonical_predictions", last=1000)
    assert first["enrollment"]["enrolled"] == 1
    assert {row["market"] for row in predictions} == {"1X2", "OVER_UNDER_2_5", "BTTS"}
    assert all(row["canonical_checkpoint"] == "T6" for row in predictions)
    assert all(row["prediction_status"] == "UNVALIDATED" for row in predictions)
    assert all(row["analysis_available"] is True for row in predictions)
    assert all(row["operationally_eligible"] is False for row in predictions)
    assert all(row["reason"] == "MODEL_VALIDATION_INSUFFICIENT" for row in predictions)
    assert all(row["validation_sample_size"] == 0 for row in predictions)
    assert all(row["validation_minimum"] == 200 for row in predictions)
    assert all(row["fair_odds"] for row in predictions)
    assert len(predictions) == 3
    assert second["captured"] == []
    assert len(repository.list_records("canonical_predictions", last=1000)) == 3


async def test_restart_does_not_duplicate_predictions():
    app, repository = runtime()
    await DataPipelineCollector(app, clock=lambda: NOW).collect_due()
    await DataPipelineCollector(app, clock=lambda: NOW).collect_due()
    assert len(repository.list_records("canonical_predictions", last=1000)) == 3


def test_t24_is_used_only_after_t6_has_been_missed():
    app, repository = runtime()
    match = app.providers.fixtures.match.model_copy(update={"kickoff": NOW + timedelta(hours=25)})
    app.providers.fixtures.match = match
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(match)
    repository.archive_pre_match_observation(
        match=match,
        as_of=NOW,
        collected_at=NOW,
        snapshots={
            "stats": app.providers.stats.stats,
            "odds": app.providers.odds.odds,
        },
        failures={},
        purpose="TEST",
        checkpoint="T24",
    )
    assert service.create_canonical_predictions(match, now=NOW) == []
    created = service.create_canonical_predictions(match, now=match.kickoff - timedelta(hours=1))
    assert len(created) == 3
    assert all(row["fallback_used"] and row["canonical_checkpoint"] == "T24" for row in created)


def test_missing_odds_keeps_match_pending():
    app, repository = runtime()
    match = app.providers.fixtures.match
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(match)
    repository.archive_pre_match_observation(
        match=match,
        as_of=NOW,
        collected_at=NOW,
        snapshots={"stats": app.providers.stats.stats},
        failures={},
        purpose="TEST",
        checkpoint="T6",
    )
    assert service.create_canonical_predictions(match, now=NOW) == []
    assert service.states()[0]["lifecycle_status"] == "DATA_INSUFFICIENT"


def test_post_kickoff_prediction_is_rejected():
    app, _ = runtime()
    match = app.providers.fixtures.match
    service = ValidationCohortService(app, clock=lambda: match.kickoff + timedelta(seconds=1))
    assert service.create_canonical_predictions(match) == []
    assert service.states()[0]["reason"] == "POST_KICKOFF_PREDICTION_REJECTED"


def test_future_feature_timestamp_is_rejected_as_leakage():
    app, repository = runtime()
    match = app.providers.fixtures.match
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(match)
    stats = app.providers.stats.stats.model_copy(update={"captured_at": NOW + timedelta(minutes=1)})
    repository.archive_pre_match_observation(
        match=match,
        as_of=NOW,
        collected_at=NOW,
        snapshots={"stats": stats, "odds": app.providers.odds.odds},
        failures={},
        purpose="TEST",
        checkpoint="T6",
    )
    assert service.create_canonical_predictions(match, now=NOW) == []
    assert service.states()[0]["reason"] == "POST_KICKOFF_FEATURE_LEAKAGE_REJECTED"


@pytest.mark.parametrize(
    ("market", "home", "away", "selection"),
    [
        (MarketType.MATCH_WINNER, 2, 1, "HOME"),
        (MarketType.MATCH_WINNER, 1, 1, "DRAW"),
        (MarketType.OVER_UNDER_2_5, 2, 1, "OVER_2_5"),
        (MarketType.OVER_UNDER_2_5, 1, 1, "UNDER_2_5"),
        (MarketType.BTTS, 2, 1, "YES"),
        (MarketType.BTTS, 2, 0, "NO"),
    ],
)
def test_regulation_time_settlement_for_supported_markets(market, home, away, selection):
    assert ValidationCohortService.actual_selection(market, home, away) == selection


async def test_automatic_result_settles_three_market_observations_once():
    provider = ResultProvider(2, 1)
    app, repository = runtime(result_provider=provider)
    clock = [NOW]
    collector = DataPipelineCollector(app, clock=lambda: clock[0])
    await collector.collect_due()
    clock[0] = app.providers.fixtures.match.kickoff + timedelta(hours=2)
    first = await collector.collect_due()
    second = await collector.collect_due()
    observations = repository.list_records("validation_observations", last=1000)
    assert len(observations) == 3
    assert {row["actual_selection"] for row in observations} == {"HOME", "OVER_2_5", "YES"}
    assert all(row["inclusion_status"] == "VALIDATION_INCLUDED" for row in observations)
    assert all(row["prediction_status"] == "UNVALIDATED" for row in observations)
    assert len(repository.settled_validation_history(clock[0])) == 3
    assert {group["sample_size"] for group in ValidationCohortService(app).report()["groups"]} == {1}
    assert len(first["results"]["settled"]) == 3
    assert second["results"]["settled"] == []


async def test_unavailable_result_remains_pending():
    app, _ = runtime(result_provider=ResultProvider(status="NONE"))
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(app.providers.fixtures.match)
    later = app.providers.fixtures.match.kickoff + timedelta(hours=2)
    summary = await service.poll_results(later)
    assert summary["pending"] == ["demo-match"]
    assert service.states()[0]["lifecycle_status"] == "RESULT_PENDING"


async def test_result_poll_can_limit_work_to_requested_matches():
    provider = ResultProvider()
    app, repository = runtime(result_provider=provider)
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(app.providers.fixtures.match)
    later = app.providers.fixtures.match.kickoff + timedelta(hours=2)
    skipped = await service.poll_results(later, match_ids={"other-match"})
    assert provider.calls == 0
    assert skipped["verified"] == []
    assert repository.list_records("match_results", last=1000) == []
    await service.poll_results(later, match_ids={"demo-match"})
    assert provider.calls == 1
    assert len(repository.list_records("match_results", last=1000)) == 1


async def test_reliable_result_conflict_prevents_settlement():
    first, second = ResultProvider(2, 1), ResultProvider(1, 2)
    composite = CompositeResultProvider(first, second)
    app, repository = runtime(result_provider=composite)
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(app.providers.fixtures.match)
    later = app.providers.fixtures.match.kickoff + timedelta(hours=2)
    summary = await service.poll_results(later)
    assert summary["conflicts"] == ["demo-match"]
    assert repository.list_records("match_results", last=1000) == []
    assert len(repository.list_records("match_result_claims", last=1000)) == 2


async def test_matching_reliable_result_sources_use_priority_order():
    first, second = ResultProvider(2, 1), ResultProvider(2, 1)
    result = await CompositeResultProvider(first, second).get_result(
        make_demo_bundle(as_of=NOW).fixtures.match,
        NOW,
    )
    assert result.provider == "official-test"
    assert result.final_status == "FINAL"


async def test_football_data_result_provider_uses_regulation_score_before_extra_time():
    record = SimpleNamespace(
        status="FINISHED",
        regular_time_home=1,
        regular_time_away=1,
        full_time_home=2,
        full_time_away=1,
        extra_time_home=2,
        extra_time_away=1,
        penalties_home=None,
        penalties_away=None,
        score_duration="EXTRA_TIME",
        provenance=SimpleNamespace(url="https://football-data.example/match/1"),
        last_updated=NOW,
        retrieved_at=NOW,
    )

    class Fixture:
        async def get_fixture_record(self, match_id):
            return record

    match = Match(
        match_id="football-data:match:1",
        competition="football-data:competition:1",
        season="2026",
        kickoff=NOW - timedelta(hours=3),
        home_team="Home",
        away_team="Away",
        home_team_id="home",
        away_team_id="away",
    )
    result = await FootballDataOrgResultProvider(Fixture()).get_result(match, NOW)
    assert (result.home_score, result.away_score) == (1, 1)
    assert (result.extra_time_home, result.extra_time_away) == (2, 1)


async def test_football_data_result_provider_leaves_unfinished_match_pending():
    class Fixture:
        async def get_fixture_record(self, match_id):
            return SimpleNamespace(status="IN_PLAY")

    match = make_demo_bundle(as_of=NOW).fixtures.match.model_copy(
        update={"match_id": "football-data:match:1"}
    )
    assert await FootballDataOrgResultProvider(Fixture()).get_result(match, NOW) is None


@pytest.mark.parametrize("status", ["IN_PLAY", "POSTPONED", "SUSPENDED", "CANCELLED"])
async def test_football_data_result_provider_ignores_non_final_statuses(status):
    class Fixture:
        async def get_fixture_record(self, match_id):
            return SimpleNamespace(status=status)

    match = make_demo_bundle(as_of=NOW).fixtures.match.model_copy(
        update={"match_id": "football-data:match:1"}
    )
    assert await FootballDataOrgResultProvider(Fixture()).get_result(match, NOW) is None


async def settled_fixture():
    app, repository = runtime(result_provider=ResultProvider())
    clock = [NOW]
    collector = DataPipelineCollector(app, clock=lambda: clock[0])
    await collector.collect_due()
    clock[0] = app.providers.fixtures.match.kickoff + timedelta(hours=2)
    await collector.collect_due()
    service = ValidationCohortService(app, clock=lambda: clock[0])
    return app, repository, service


async def test_closing_price_uses_last_pre_kickoff_quote():
    _, repository, _ = await settled_fixture()
    rows = repository.list_records("validation_observations", last=1000)
    assert all(row["closing_price_status"] == "AVAILABLE" for row in rows)
    assert all(all(value is not None for value in row["closing_odds"].values()) for row in rows)


async def test_missing_closing_price_does_not_block_quality_settlement():
    source_app, source_repository = runtime()
    await DataPipelineCollector(source_app, clock=lambda: NOW).collect_due()
    predictions = source_repository.list_records("canonical_predictions", last=1000)

    target_app, target_repository = runtime(result_provider=ResultProvider())
    match = target_app.providers.fixtures.match
    target_repository.save_match(match)
    cohort = ValidationCohortService(target_app, clock=lambda: match.kickoff + timedelta(hours=2))
    cohort.enroll(match, now=NOW)
    for prediction in predictions:
        identity = (
            f"{match.match_id}:{prediction['model_name']}:"
            f"{prediction['model_version']}:{prediction['market']}"
        )
        with target_repository.engine.begin() as connection:
            target_repository._append(
                connection,
                "canonical_predictions",
                identifier=prediction["prediction_id"],
                entity_id=identity,
                match_id=match.match_id,
                payload=prediction,
            )
    result = await target_app.providers.results.get_result(match, match.kickoff + timedelta(hours=2))
    cohort.record_result(match, result, match.kickoff + timedelta(hours=2))
    rows = target_repository.list_records("validation_observations", last=1000)
    assert len(rows) == 3
    assert all(row["closing_price_status"] == "CLOSING_PRICE_MISSING" for row in rows)


async def test_dashboard_summary_separates_quality_and_betting_metrics():
    _, _, service = await settled_fixture()
    report = service.report()
    assert report["valid_observations"] == 3
    assert len(report["groups"]) == 3
    assert all("model_quality" in group and "betting_performance" in group for group in report["groups"])
    assert all(group["progress"] == "1 / 200" for group in report["groups"])


async def test_market_feature_and_config_groups_do_not_mix():
    _, repository, service = await settled_fixture()
    original = repository.list_records("validation_observations", last=1000)[0]
    for index, changes in enumerate(
        (
            {"model_version": "1.1.0"},
            {"feature_schema_version": "2.0.0"},
            {"config_hash": "different"},
        ),
        start=1,
    ):
        row = {**original, **changes, "prediction_id": f"variant-{index}", "match_id": f"variant-{index}"}
        with repository.engine.begin() as connection:
            repository._append(
                connection,
                "validation_observations",
                identifier=f"variant-observation-{index}",
                entity_id=row["match_id"],
                match_id=row["match_id"],
                payload=row,
            )
    report = service.report()
    assert len(report["groups"]) == 6


async def test_199_does_not_pass_and_200_reaches_minimum_without_lowering_threshold():
    _, repository, service = await settled_fixture()
    original = next(
        row
        for row in repository.list_records("validation_observations", last=1000)
        if row["market"] == "BTTS"
    )
    for index in range(1, 199):
        row = {**original, "prediction_id": f"sample-{index}", "match_id": f"sample-{index}"}
        with repository.engine.begin() as connection:
            repository._append(
                connection,
                "validation_observations",
                identifier=f"sample-observation-{index}",
                entity_id=row["match_id"],
                match_id=row["match_id"],
                payload=row,
            )
    group = next(row for row in service.report()["groups"] if row["market"] == "BTTS")
    assert group["sample_size"] == 199
    assert group["validation_status"] == "INSUFFICIENT_SAMPLE"
    row = {**original, "prediction_id": "sample-199", "match_id": "sample-199"}
    with repository.engine.begin() as connection:
        repository._append(
            connection,
            "validation_observations",
            identifier="sample-observation-199",
            entity_id=row["match_id"],
            match_id=row["match_id"],
            payload=row,
        )
    group = next(row for row in service.report()["groups"] if row["market"] == "BTTS")
    assert group["sample_size"] == 200
    assert group["progress_status"] == "CAMPIONE MINIMO RAGGIUNTO"
    assert group["validation_status"] != "INSUFFICIENT_SAMPLE"


def test_research_budget_blocks_additional_match_cost():
    app, repository = runtime(configured=settings(validation_research_budget_per_match=0.10))
    payload = {
        "match_id": "demo-match",
        "checkpoint": "T6",
        "at": NOW.isoformat(),
        "estimated_cost": 0.10,
        "tool_calls": 1,
        "input_tokens": 10,
        "output_tokens": 10,
    }
    with repository.engine.begin() as connection:
        repository._append(
            connection,
            "validation_cost_events",
            identifier=canonical_hash(payload),
            entity_id="demo-match",
            match_id="demo-match",
            payload=payload,
        )
    service = ValidationCohortService(app, clock=lambda: NOW)
    assert service._within_research_budget("demo-match", NOW) is False


def test_historical_backtest_is_not_counted_in_live_cohort_report():
    app, repository = runtime()
    row = {
        "match_id": "historical",
        "model_name": "poisson",
        "model_version": "1.0.0",
        "market": "BTTS",
        "feature_schema_version": "1.0.0",
        "config_hash": "x",
        "source_type": "HISTORICAL_BACKTEST",
        "inclusion_status": "VALIDATION_INCLUDED",
    }
    with repository.engine.begin() as connection:
        repository._append(
            connection,
            "validation_observations",
            identifier="historical-only",
            entity_id="historical",
            match_id="historical",
            payload=row,
        )
    report = ValidationCohortService(app).report()
    assert report["valid_observations"] == 0
    assert report["groups"] == []


def test_match_detail_exposes_technical_ids_and_inclusion_state():
    app, _ = runtime()
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(app.providers.fixtures.match)
    detail = service.report()["matches"][0]
    assert detail["match_id"] == "demo-match"
    assert detail["competition"] == "SYNTHETIC_LEAGUE"
    assert detail["eligibility_status"] == "VALIDATION_ELIGIBLE"

def test_hidden_cohort_rows_do_not_remove_history_or_change_validator_counts():
    app, repository = runtime()
    service = ValidationCohortService(app, clock=lambda: NOW)
    service.enroll(app.providers.fixtures.match)
    service.enroll(app.providers.fixtures.match.model_copy(update={'match_id': 'second-match'}))
    before = service.report()
    states = service.states()
    with repository.engine.begin() as conn:
        repository._append(conn, 'annotations', identifier='cohort-hidden:demo-match', entity_id='cohort:hidden:v1', match_id='demo-match', payload={'kind': 'HIDDEN', 'match_id': 'demo-match', 'reason': 'Legacy unresolved test', 'at': NOW.isoformat()})
    after = service.report()
    assert len(before['matches']) == 2 and len(after['matches']) == 1
    assert after['matches'][0]['match_id'] == 'second-match'
    assert after['hidden_matches'] == 1
    assert service.states() == states
    assert {k:v for k,v in before.items() if k not in {'matches', 'hidden_matches'}} == {k:v for k,v in after.items() if k not in {'matches', 'hidden_matches'}}
    assert service.report() == after
    repository.close()
