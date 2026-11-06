"""Generate real offline API contracts for the JavaScript controller tests."""

import json
import logging
import os
from uuid import uuid4

from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.services.application import make_application
from better.storage.repository import Repository
from better.system_one import SystemOneSettings


def main():
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
                "/metrics",
                "/bank",
                "/api-control",
                "/data-pipeline",
                "/salabet/candidates",
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


if __name__ == "__main__":
    main()
