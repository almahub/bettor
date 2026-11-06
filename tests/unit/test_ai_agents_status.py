from types import SimpleNamespace

from better.services.ai_agents_status import ai_agents_report
from better.storage.repository import Repository


def service(agent_id, *, enabled=True, ready=True):
    return SimpleNamespace(
        settings=SimpleNamespace(max_cost_per_match="0.10", recent_form_ttl_seconds=1800),
        describe=lambda: {"agent_id": agent_id, "enabled": enabled, "ready": ready,
                          "transport": "agents_api", "fields": ["recent_form"]},
    )


def test_status_uses_current_agent_history_even_when_usage_is_absent(monkeypatch):
    repo = Repository("sqlite:///:memory:")
    runtime = SimpleNamespace(repository=repo, external_research=service("agent-context"),
                              football_data_research=service("agent-data"))
    monkeypatch.setenv("OPENAI_API_KEY", "private-value-for-test")
    with repo.engine.begin() as c:
        for identifier, agent, status, date in [
            ("old", "agent-context", "OK", "2026-10-01T12:00:00Z"),
            ("current", "agent-context", "RESEARCH_NOT_FOUND", "2026-10-02T12:00:00Z"),
            ("other", "different-agent", "EXTERNAL_RESEARCH_ERROR", "2026-10-03T12:00:00Z"),
        ]:
            repo._append(c, "research_results", identifier=identifier, entity_id=identifier, match_id="m1",
                         payload={"request_id": identifier, "agent_id": agent, "status": status,
                                  "match_id": "m1", "completed_at": date, "findings": []})
        repo._append(c, "annotations", identifier="data-error", entity_id="data-error", match_id="m2",
                     payload={"request_id": "data-error", "agent_id": "agent-data", "status": "FOOTBALL_DATA_RESEARCH_ERROR",
                              "match_id": "m2", "completed_at": "2026-10-02T12:00:00Z", "requested_fields": ["recent_form"],
                              "error": "HTTP 401 private-value-for-test"})
    report = ai_agents_report(runtime)
    context, data = report["agents"]
    assert report["verification"] == "LOCAL_RECORDS_ONLY"
    assert context["configured"] and data["configured"]
    assert context["recorded_runs"] == 2
    assert context["latest"]["status"] == "RESEARCH_NOT_FOUND"
    assert data["latest"]["status"] == "FOOTBALL_DATA_RESEARCH_ERROR"
    assert "private-value-for-test" not in str(report)
    assert len(repo.all_records("research_results")) == 3
    repo.close()


def test_disabled_or_missing_agent_is_not_reported_working():
    repo = Repository("sqlite:///:memory:")
    runtime = SimpleNamespace(repository=repo, external_research=service(None, ready=False),
                              football_data_research=service("agent-data", enabled=False))
    report = ai_agents_report(runtime)
    assert all(not r["configured"] and r["latest"] is None for r in report["agents"])
    repo.close()
