import json
import os
import subprocess
import sys


def test_cli_console_and_json_file_distinguish_collection_execution_and_limitations(tmp_path):
    logfile = tmp_path / "events.jsonl"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "better.cli.main",
            "--database",
            "sqlite:///:memory:",
            "--log-file",
            str(logfile),
            "analyze",
            "--match",
            "demo-match",
            "--demo",
            "bet",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "SYSTEM_ONE_ENABLED": "false"},
    )
    assert completed.returncode == 0, completed.stderr
    assert "[SOURCE" in completed.stderr
    assert "[VALIDATE" in completed.stderr
    assert "WARNING_ONLY: HISTORICAL_BANKROLL_UNAVAILABLE" in completed.stderr
    assert "FINAL STATUS: BET" in completed.stdout
    assert "CONFIDENCE COMPONENTS" in completed.stdout
    assert "Missing checks: bankroll_roi, bankroll_drawdown" in completed.stdout
    records = [json.loads(line) for line in logfile.read_text(encoding="utf-8").splitlines()]
    agents = [r for r in records if r["event"] == "orchestrator_agent_result_received"]
    assert len(agents) == len({r["agent"] for r in agents}) == 13
    assert not any(r["event"] in {"agent_completed", "agent_execution_finished"} for r in records)
    sources = [r for r in records if r["event"] == "source_collection_completed"]
    assert {r["source_kind"] for r in sources} == {"stats", "odds", "news", "lineups"}
    assert all(r["provider"] == "FakeProvider" and r["duration_ms"] >= 0 for r in sources)
    validation = next(r for r in agents if r["agent"] == "model_validator")
    assert validation["degradations"][0]["impact"] == "WARNING_ONLY"
    assert validation["degradations"][0]["missing_checks"] == ["bankroll_roi", "bankroll_drawdown"]
