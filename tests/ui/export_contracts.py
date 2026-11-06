"""Generate real offline API contracts for the JavaScript controller tests."""

import json
import logging
import os
import tempfile
from contextlib import chdir
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.services.application import make_application
from better.storage.repository import Repository
from better.system_one import SystemOneSettings


def generate_contracts():
    os.environ["OPENAI_USAGE_LIVE_ENABLED"] = "false"
    engine = make_application(
        settings=Settings(),
        repository=Repository("sqlite:///:memory:"),
        demo="bet",
        system_one_settings=SystemOneSettings(enabled=False),
    )
    with TestClient(create_app(engine)) as client:
        logging.disable(logging.CRITICAL)
        result = {
            path: client.get(path).json()
            for path in (
                "/health",
                "/workspace",
                "/system/ai-agents",
                "/matches",
                "/models",
                "/models/diagnostics",
                "/metrics",
                "/bank",
                "/api-control",
                "/data-pipeline",
                "/salabet/candidates",
                "/salabet/automatic-multiples",
                "/salabet/slips",
            )
        }
        result["runs"] = {
            scenario: client.post("/demo/analysis", json={"scenario": scenario}).json()
            for scenario in ("bet", "no_bet", "wait_lineups", "wait_odds", "watchlist", "missing_data")
        }
        result["/analyses?last=50"] = client.get("/analyses?last=50").json()
        from better.services.bank_service import BankInitialize, BankService

        result["bank_initialized"] = BankService(engine.repository).initialize(
            BankInitialize(request_id=uuid4(), amount="100")
        )
        print(json.dumps(result))


def main():
    # Contract tests must not read the developer's account exports or write to its data directory.
    os.environ["OPENAI_COST_USD_EUR_RATE"] = "0.90"
    with tempfile.TemporaryDirectory(prefix="bettor-ui-contracts-") as directory, chdir(directory):
        exports = Path("data/api-control")
        exports.mkdir(parents=True)
        usage = {
            "data": [
                {
                    "start_time": 1790812800,
                    "results": [
                        {
                            "model": "test-model",
                            "api_source": "agents_api",
                            "num_model_requests": 2,
                            "input_tokens": 100,
                            "output_tokens": 10,
                        }
                    ],
                }
            ]
        }
        costs = {
            "data": [
                {
                    "start_time": 1790812800,
                    "results": [{"amount": {"value": "1.25", "currency": "usd"}, "line_item": "test usage"}],
                }
            ]
        }
        (exports / "completions-usage.json").write_text(json.dumps(usage), encoding="utf-8")
        (exports / "costs.json").write_text(json.dumps(costs), encoding="utf-8")
        generate_contracts()


if __name__ == "__main__":
    main()
