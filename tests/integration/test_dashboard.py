import mimetypes

import pytest
from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.services.application import make_application
from better.storage.repository import Repository
from better.system_one import SystemOneSettings


def runtime(*, demo="bet", limit=20):
    return make_application(
        settings=Settings(analysis_requests_per_minute=limit),
        repository=Repository("sqlite:///:memory:"),
        demo=demo,
        system_one_settings=SystemOneSettings(enabled=False),
    )


def test_journal_bulk_removal_preserves_audit_and_refills_list():
    from uuid import uuid4

    engine = runtime()
    with TestClient(create_app(engine)) as client:
        first = client.post("/demo/analysis", json={"scenario": "bet"}).json()
        second = client.post("/demo/analysis", json={"scenario": "no_bet"}).json()
        ids = [second["analysis_id"], first["analysis_id"]]
        before_bank = client.get("/bank").json()
        before_journal = client.get("/journal").json()
        assert client.post("/analyses/remove", json={"analysis_ids": []}).status_code == 422
        assert (
            client.post("/analyses/remove", json={"analysis_ids": [ids[0], str(uuid4())]}).status_code == 404
        )
        assert len(client.get("/analyses").json()) == 2
        assert len(client.get("/analyses?last=1&offset=0").json()) == 1
        assert client.get("/analyses?last=1&offset=1").json()[0]["analysis_id"] == ids[1]
        assert (
            client.post(
                "/analyses/remove", json={"analysis_ids": ids}, headers={"origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert client.post("/analyses/remove", json={"analysis_ids": [ids[0]]}).status_code == 200
        assert client.get("/analyses?last=1").json()[0]["analysis_id"] == ids[1]
        for _ in range(2):
            assert client.post("/analyses/remove", json={"analysis_ids": ids}).status_code == 200
        assert client.get("/analyses").json() == []
        for identifier in ids:
            assert client.get(f"/analysis/{identifier}").status_code == 200
        assert client.get("/journal").json() == before_journal
        assert client.get("/bank").json() == before_bank
    with TestClient(create_app(engine)) as client:
        assert client.get("/analyses").json() == []


def test_same_origin_dashboard_assets_and_safe_workspace(monkeypatch):
    # Some Windows registry associations expose modules as plain text.
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".mjs", "text/plain")
    with TestClient(create_app(runtime())) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'lang="it"' in response.text
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        for asset in ("dashboard.css", "dashboard.mjs", "ui.mjs", "favicon.svg"):
            result = client.get(f"/assets/{asset}")
            assert result.status_code == 200
            if asset.endswith("mjs"):
                assert "javascript" in result.headers["content-type"]
                assert result.headers["cache-control"] == "no-cache"
                assert result.headers["x-content-type-options"] == "nosniff"
        workspace = client.get("/workspace").json()
        assert workspace["is_demo"] is True
        assert len(workspace["agents"]) == 14
        assert len([a for a in workspace["agents"] if a["separate_workflow"]]) == 1
        assert all(
            agent["responsibility"] and agent["data_inputs"] and agent["output"]
            for agent in workspace["agents"]
        )
        assert all("allowed_tools" in agent for agent in workspace["agents"])
        assert workspace["external_research"]["enabled"] is False
        assert "database_url" not in workspace
        assert "sources" not in workspace
        assert client.get("/assets/../config/sources.yaml").status_code == 404


def test_api_control_reports_sanitized_local_exports():
    with TestClient(create_app(runtime())) as client:
        report = client.get("/api-control").json()
        assert report["source"] == "LOCAL_EXPORT"
        assert report["summary"]["requests"] == 157
        assert report["summary"]["total_tokens"] == 1_936_111
        assert report["summary"]["costs"]["USD"].startswith("0.97724839")
        serialized = str(report)
        assert "api_key_id" not in serialized
        assert "user_id" not in serialized
        assert "organization_id" not in serialized


def test_data_pipeline_control_plane_is_available():
    with TestClient(create_app(runtime())) as client:
        report = client.get("/data-pipeline").json()
        assert report["status"] == "READY"
        assert report["policy"]["selection"] == "SELECTED_MATCHES_ONLY"
        assert report["policy"]["database_records_are_immutable"] is True
        assert len(report["agent_coverage"]["agents"]) == 14
        assert report["storage"]["within_limit"] is True
        collection = client.post("/data-pipeline/collect-due", json={})
        assert collection.status_code == 200
        payload = collection.json()
        assert {key: payload[key] for key in ("selected", "captured", "skipped", "failed")} == {
            "selected": 0,
            "captured": [],
            "skipped": [],
            "failed": [],
        }
        assert payload["cohort"] == 0
        assert "enrollment" in payload and "results" in payload


def test_manual_result_identity_requires_confirmation_and_is_append_only():
    engine = runtime()
    match = engine.providers.fixtures.match
    engine.repository.save_match(match)
    payload = {
        "source_match_id": match.match_id,
        "provider": "football_data_org",
        "provider_match_id": "football-data:match:999",
        "provider_competition_id": "football-data:competition:2123",
        "provider_competition_name": "Serie C",
        "competition_group": "GROUP_A",
        "provider_home_team_id": "football-data:team:1",
        "provider_away_team_id": "football-data:team:2",
        "provider_home_team_name": match.home_team,
        "provider_away_team_name": match.away_team,
        "provider_kickoff": match.kickoff.isoformat(),
        "source_tier": "B",
        "source_url": "https://www.seriec.com/calendario",
        "verified_by": "operatore test",
        "notes": "Identità confrontata con il calendario ufficiale",
        "confirmed": True,
    }
    with TestClient(create_app(engine)) as client:
        rejected = client.post(
            "/data-pipeline/result-identities/manual", json={**payload, "confirmed": False}
        )
        accepted = client.post("/data-pipeline/result-identities/manual", json=payload)
        repeated = client.post("/data-pipeline/result-identities/manual", json=payload)
    assert rejected.status_code == 409
    assert accepted.status_code == repeated.status_code == 200
    rows = [
        row
        for row in engine.repository.list_records("identity_mappings", last=1000)
        if row.get("scope") == "result_identity"
    ]
    assert len(rows) == 4
    assert next(row for row in rows if row["kind"] == "match")["verification_method"] == "MANUAL_VERIFIED"


@pytest.mark.parametrize(
    "scenario,status",
    [
        ("bet", "BET"),
        ("no_bet", "NO_BET"),
        ("wait_lineups", "WAIT_FOR_LINEUPS"),
        ("wait_odds", "WAIT_FOR_BETTER_ODDS"),
        ("watchlist", "WATCHLIST"),
        ("missing_data", "INSUFFICIENT_DATA"),
    ],
)
def test_six_decisions_flow_through_dashboard_and_persist(scenario, status):
    engine = runtime()
    original_providers = engine.providers
    with TestClient(create_app(engine)) as client:
        response = client.post("/demo/analysis", json={"scenario": scenario})
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["decision"]["status"] == status
        assert engine.providers is original_providers
        assert client.get("/health").json()["system_one"]["enabled"] is False
        summary = client.get("/analyses").json()[0]
        assert summary["analysis_id"] == run["analysis_id"]
        assert summary["status"] == status
        assert summary["is_demo"] is True
        assert summary["stake"] == run["decision"]["stake"]
        assert "input_manifest" not in summary
        assert client.get(f"/analysis/{run['analysis_id']}").json() == run
        assert client.get("/journal").json()[0]["analysis_id"] == run["analysis_id"]
        assert client.get("/metrics").json()["pipeline"]["decision_counts"][status] == 1


def test_demo_never_activates_in_unconfigured_real_workspace():
    with TestClient(create_app(runtime(demo=None))) as client:
        workspace = client.get("/workspace").json()
        assert workspace["is_demo"] is False
        assert workspace["demo_cases"] == []
        assert client.post("/demo/analysis", json={"scenario": "bet"}).status_code == 403
        assert client.get("/analyses").json() == []


def test_demo_validation_and_shared_rate_limit():
    with TestClient(create_app(runtime(limit=1))) as client:
        assert client.post("/demo/analysis", json={"scenario": "made-up"}).status_code == 422
        assert client.get("/analyses?last=0").status_code == 422
        assert client.get("/analyses?last=101").status_code == 422
        assert client.post("/demo/analysis", json={"scenario": "bet"}).status_code == 200
        response = client.post("/analysis", json={"match_id": "demo-match"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
