import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.domain.schemas import AnalysisRun
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.storage.repository import Repository
from better.system_one import SystemOneSettings


@pytest.fixture
def client():
    now = datetime.now(timezone.utc)
    bundle = make_demo_bundle("bet", as_of=now)
    runtime = make_application(
        settings=Settings(analysis_requests_per_minute=2),
        providers=bundle,
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(),
    )
    with TestClient(create_app(runtime)) as client:
        yield client, now, runtime


def test_all_api_endpoints_and_round_trip(client):
    client, now, runtime = client
    assert client.get("/health").json()["system_one"]["status"] == "disabled"
    assert client.get("/matches").json()[0]["match_id"] == "demo-match"
    assert client.get("/matches/demo-match").status_code == 200
    request = {"match_id": "demo-match", "as_of": now.isoformat(), "markets": ["1X2"]}
    response = client.post("/analysis", json=request)
    assert response.status_code == 200, response.text
    run = AnalysisRun.model_validate(response.json())
    assert run.decision.status.value == "BET"
    identifier = str(run.analysis_id)
    assert client.get(f"/analysis/{identifier}").json() == response.json()
    assert len(client.get(f"/analysis/{identifier}/agents").json()) == 13
    assert client.get(f"/analysis/{identifier}/decision").json()["status"] == "BET"
    report = client.get(f"/analysis/{identifier}/report")
    assert "SYNTHETIC" in report.text and "FINAL STATUS: BET" in report.text
    assert client.get(f"/analysis/{identifier}/shadow").json() == []
    assert client.get("/models").json()[0]["model"] == "poisson"
    assert client.get("/models/poisson/validation").json()
    assert client.get("/journal").json()[0]["phase"] == "PRE_MATCH"
    assert client.get("/metrics").json()["pipeline"]["decision_counts"] == {"BET": 1}
    duplicate = client.post("/analysis", json=request)
    assert duplicate.json() == response.json()
    assert len(runtime.repository.list_records("journal_entries")) == 1
    assert client.post("/analysis", json=request).status_code == 429


def test_api_invalid_requests_and_missing_records(client):
    client, now, runtime = client
    assert client.get(f"/analysis/{uuid4()}").status_code == 404
    assert client.get("/analysis/not-a-uuid").status_code == 422
    assert client.get("/matches/unknown").status_code == 404
    assert (
        client.post("/analysis", json={"match_id": "demo-match", "markets": ["PLAYER_PROPS"]}).status_code
        == 422
    )
    assert (
        client.post("/analysis", json={"match_id": "demo-match", "as_of": "2026-01-01T00:00:00"}).status_code
        == 422
    )
    assert client.get("/journal?last=0").status_code == 422
    assert client.post("/analysis", json={"match_id": "demo-match", "mode": "LIVE"}).status_code == 422


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "better.cli.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env=env,
    )


def test_installed_cli_demo_report_json_and_journal(tmp_path):
    database = "sqlite:///" + (tmp_path / "cli.db").as_posix()
    response = run_cli("--database", database, "analyze", "--match", "demo-match", "--demo", "bet", "--json")
    assert response.returncode == 0, response.stderr
    run = AnalysisRun.model_validate_json(response.stdout)
    assert run.decision.status.value == "BET"
    assert run.input_manifest["request"]["as_of"] is None
    response = run_cli("--database", database, "journal", "--last", "5")
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)[0]["snapshot"]["decision"]["status"] == "BET"
    response = run_cli("--database", database, "analyze", "--match", "demo-match", "--demo", "wait_lineups")
    assert response.returncode == 0, response.stderr
    assert "FINAL STATUS: WAIT_FOR_LINEUPS" in response.stdout


def test_cli_kill_switch_and_unconfigured_sources(tmp_path):
    database = "sqlite:///" + (tmp_path / "empty.db").as_posix()
    env = {**os.environ, "SYSTEM_ONE_ENABLED": "false"}
    response = run_cli("--database", database, "health", env=env)
    assert response.returncode == 0
    assert json.loads(response.stdout)["system_one"]["status"] == "disabled"
    response = run_cli("--database", database, "analyze", "--match", "a-real-match-without-source", env=env)
    assert response.returncode != 0
    assert "No fixture source configured" in response.stderr


def test_cli_shadow_replay_uses_stored_state(tmp_path):
    database = "sqlite:///" + (tmp_path / "shadow.db").as_posix()
    env = {
        **os.environ,
        "SYSTEM_ONE_ENABLED": "true",
        "SYSTEM_ONE_PROVIDER": "fake",
        "SYSTEM_ONE_SHADOW_MODE": "true",
    }
    response = run_cli(
        "--database", database, "analyze", "--match", "demo-match", "--demo", "bet", "--json", env=env
    )
    assert response.returncode == 0, response.stderr
    identifier = json.loads(response.stdout)["analysis_id"]
    replayed = run_cli("--database", database, "replay", "--analysis-id", identifier, env=env)
    assert replayed.returncode == 0, replayed.stderr
    assert len(json.loads(replayed.stdout)) == 4
    assert all(item["changed_questions"] == [] for item in json.loads(replayed.stdout))


def test_batch_api_supports_all_selection_report_and_refresh():
    now = datetime.now(timezone.utc)
    runtime = make_application(
        settings=Settings(analysis_requests_per_minute=20),
        providers=make_demo_bundle("bet", as_of=now),
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(),
    )
    with TestClient(create_app(runtime)) as api:
        response = api.post(
            "/batch-analysis",
            json={"mode": "SELECTION", "match_ids": ["demo-match"], "filter": {"competition": "all"}},
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["transferred"] == 1
        assert api.get(f"/batch-analysis/{report['batch_id']}").json()["rows"] == report["rows"]
        refresh = api.post(f"/batch-analysis/{report['batch_id']}/refresh", json={})
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()["parent_batch_id"] == report["batch_id"]
        assert api.get("/salabet/slips").json() == []
