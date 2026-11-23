import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import yaml

from better.agents.registry import create_registry
from better.config import Settings
from better.domain.enums import AgentStatus, MarketType
from better.domain.schemas import AgentContext
from better.providers.fake import make_demo_bundle
from better.providers.free_football import (
    FreeFootballProvider,
    FreeFootballSettings,
    parse_football_data_csv,
    parse_openfootball,
)
from better.providers.result_identity import DeterministicResultIdentityResolver, ResultIdentityCandidate
from better.providers.results import CompositeResultProvider, FootballDataOrgResultProvider
from better.services.league_configuration import PREMIER, SERIE_A, SERIE_B, serie_a_premier
from better.services.league_retirement import purge_retired_records, retire_serie_b
from better.storage.repository import Repository

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def test_league_profile_is_idempotent_and_preserves_budgets_and_switches():
    root = Path(__file__).parents[2] / "config/888"
    original_app = yaml.safe_load((root / "app.yaml").read_text(encoding="utf-8"))
    original_sources = yaml.safe_load((root / "sources.yaml").read_text(encoding="utf-8"))
    original_app.update(timing_comparison_enabled=False, validation_research_budget_per_match=0.25)
    before = json.dumps([original_app, original_sources], sort_keys=True)
    app, sources = serie_a_premier(original_app, original_sources)
    assert serie_a_premier(app, sources) == (app, sources)
    assert not app["timing_comparison_enabled"]
    assert app["validation_research_budget_per_match"] == 0.25
    assert json.dumps([original_app, original_sources], sort_keys=True) == before
    assert app["active_competitions"] == [SERIE_A, PREMIER]
    assert SERIE_B not in sources["free_football"]["competitions"]
    assert sources["football_data_research"]["max_cost_per_match"] == original_sources["football_data_research"]["max_cost_per_match"]
    assert sources["api_football"]["network_enabled"] is False


def test_premier_timezone_and_csv_observations_are_not_italian_or_invented():
    rows = parse_openfootball({"name": "English Premier League 2026/27", "matches": [
        {"date": "2026-08-21", "time": "20:00", "team1": "Home", "team2": "Away"}]}, "E0", 2026)
    assert rows[0]["kickoff"] == "2026-08-21T19:00:00+00:00"
    text = "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,HS,AS,HST,AST\n"
    text += "E0,08/11/2026,15:00,Home,Away,2,0,12,,5,0\n"
    text += "E0,09/11/2026,,A,B,1,1,1,1,1,1\n"
    parsed = parse_football_data_csv(text, "E0", 2026, "https://football-data.co.uk/season.csv")
    assert len(parsed) == 1
    assert parsed[0]["statistics"] == {"home_shots": 12, "home_shots_on_target": 5, "away_shots_on_target": 0}
    with pytest.raises(ValueError, match="competition"):
        parse_football_data_csv(text, "I1", 2026, "https://source.test/data.csv")
    with pytest.raises(ValueError, match="season"):
        parse_football_data_csv(text, "E0", 2025, "https://source.test/data.csv")


async def test_premier_archive_is_local_deduplicated_and_known_at_not_backdated():
    repo = Repository("sqlite:///:memory:")
    match = make_demo_bundle(as_of=NOW).fixtures.match.model_copy(update={"competition": PREMIER})
    fixture = SimpleNamespace(get_match=AsyncMock(return_value=match))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected network")))
    provider = FreeFootballProvider(FreeFootballSettings(competitions={PREMIER: "E0"}),
        repository=repo, fixtures=fixture, client=client)
    observations = [{"division": division, "season": 2026, "kickoff": (NOW-timedelta(days=i+1)).isoformat(),
        "home": match.home_team, "away": match.away_team, "home_score": score, "away_score": 0,
        "statistics": {}} for i in range(10) for division, score in (("E0", 2), ("I1", 9))]
    provider.save_rows(observations, "https://source.test/season", NOW)
    provider.save_rows(observations, "https://source.test/season", NOW)
    assert len(repo.all_records("annotations")) == 4
    assert provider.rows(NOW-timedelta(seconds=1)) == []
    delayed = [{**r, "kickoff": (datetime.fromisoformat(r["kickoff"])+timedelta(minutes=10)).isoformat()}
               for r in observations if r["division"] == "E0"]
    provider.save_rows(delayed, "https://other-source.test/season", NOW)
    snapshot = await provider.get_stats(match.match_id, NOW)
    assert snapshot.home.sample_size == snapshot.away.sample_size == 10
    assert snapshot.home.goals_for_per_match == 2
    assert all(r.competition_id.startswith("free-football:E0:") for r in snapshot.home.recent_matches)
    assert not repo.all_records("validation_observations")
    await provider.close()
    repo.close()


async def test_historical_sources_are_reused_without_redownloading_within_check_interval():
    repo = Repository("sqlite:///:memory:")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Repeated historic download")))
    provider = FreeFootballProvider(FreeFootballSettings(openfootball_divisions=("E0",), csv_divisions=("E0",), seasons=(2025,)),
        repository=repo, client=client, clock=lambda: NOW)
    row = {"division": "E0", "season": 2025, "kickoff": "2026-05-01T14:00:00+00:00",
        "home": "Home", "away": "Away", "home_score": 1, "away_score": 0, "statistics": {}}
    for url in ("https://raw.githubusercontent.com/openfootball/football.json/master/2025-26/en.1.json",
                "https://football-data.co.uk/mmz4281/2526/E0.csv"):
        provider.save_rows([row], url, NOW-timedelta(days=1))
    assert (await provider.refresh_archive())["requests"] == 0
    await provider.close()
    repo.close()


async def test_reviewed_aliases_still_require_oriented_teams_kickoff_and_competition():
    repo = Repository("sqlite:///:memory:")
    source = make_demo_bundle(as_of=NOW).fixtures.match.model_copy(update={
        "competition": PREMIER, "home_team": "Tottenham", "away_team": "Brighton"})
    target = ResultIdentityCandidate(provider="football_data_org", source_tier="B", provider_match_id="football-data:match:100",
        competition_id="football-data:competition:2021", competition_name="Premier League",
        home_team_id="football-data:team:1", away_team_id="football-data:team:2", home_team_name="Tottenham Hotspur FC",
        away_team_name="Brighton & Hove Albion FC", kickoff=source.kickoff, source_url="https://api.football-data.org/v4/matches/100")
    provider = FootballDataOrgResultProvider(None, competitions={PREMIER: target.competition_id},
        team_aliases={"tottenham hotspur": "tottenham", "brighton hove albion": "brighton"})
    provider.find_candidates = AsyncMock(return_value=(target,))
    resolver = DeterministicResultIdentityResolver(repo, CompositeResultProvider(provider))
    result = await resolver.resolve(source, NOW)
    assert result.verified
    assert (await resolver.resolve(source, NOW)).resolution_method == "PERSISTED_MATCH_MAPPING"
    for update in ({"competition": SERIE_A}, {"kickoff": source.kickoff+timedelta(hours=1)},
                   {"home_team": "Brighton", "away_team": "Tottenham", "match_id": "reversed",
                    "home_team_id": source.away_team_id, "away_team_id": source.home_team_id}):
        assert not (await resolver.resolve(source.model_copy(update=update), NOW)).verified
    assert len(repo.all_records("identity_mappings")) == 4
    repo.close()


async def test_model_validator_does_not_admit_premier_using_serie_a_samples():
    bundle = make_demo_bundle(as_of=NOW)
    context = AgentContext(analysis_id=uuid4(), run_id=uuid4(),
        match=bundle.fixtures.match.model_copy(update={"competition": PREMIER}),
        requested_markets=(MarketType.MATCH_WINNER,), as_of=NOW,
        validation_history=tuple(r.model_copy(update={"competition": SERIE_A}) for r in bundle.validation_history))
    result = await create_registry(bundle, Settings()).get("model_validator").run(context)
    assert result.status == AgentStatus.INSUFFICIENT_DATA
    one = bundle.validation_history[0].model_copy(update={"competition": PREMIER})
    context = context.model_copy(update={"validation_history": (*context.validation_history, one)})
    result = await create_registry(bundle, Settings()).get("model_validator").run(context)
    assert result.payload.validations[0].sample_size == 1


def test_retirement_preserves_cost_ledger_and_shared_a_fixtures_and_restores_triggers(tmp_path):
    path = tmp_path / "isolated.db"
    repo = Repository("sqlite:///" + path.as_posix())
    with repo.engine.begin() as c:
        for identifier, match_id, payload, table in [
            ("b", "b", {"match_id": "b", "competition": SERIE_B}, "matches"),
            ("a", "a", {"match_id": "a", "competition": SERIE_A}, "matches"),
            ("budget", "b", {"reservation_id": "r", "committed_usd": ".25"}, "annotations"),
            ("cost", "b", {"cost": ".25"}, "validation_cost_events"),
            ("a-catalog", None, {"endpoint": "fixtures", "params": {"league": 135, "season": 2026},
                "data": [{"teams": {"home": {"id": 1}, "away": {"id": 2}}}]}, "api_football_archive"),
            ("b-shared", None, {"endpoint": "fixtures", "params": {"id": 3},
                "data": [{"league": {"id": 136}, "teams": {"home": {"id": 1}, "away": {"id": 3}}}]}, "api_football_archive"),
            ("b-only", None, {"endpoint": "fixtures", "params": {"league": 136}, "data": []}, "api_football_archive"),
        ]:
            repo._append(c, table, identifier=identifier, entity_id=identifier, match_id=match_id, payload=payload)
    repo.close()
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        report = purge_retired_records(conn)
        assert report["matches_removed"] == 1
        assert json.loads(conn.execute("SELECT payload FROM matches").fetchone()[0])["match_id"] == "a"
        assert conn.execute("SELECT count(*) FROM validation_cost_events").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM annotations").fetchone()[0] == 1
        assert {r[0] for r in conn.execute("SELECT id FROM api_football_archive")} == {"a-catalog", "b-shared"}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM matches")


def test_retirement_requires_same_installation_exclusive_lock_before_backup(tmp_path):
    with pytest.raises(ValueError, match="blocco backend"):
        retire_serie_b(tmp_path, backend_lock=SimpleNamespace(acquired=False), safety_directory=tmp_path/"safety")
    assert not list(tmp_path.iterdir())
