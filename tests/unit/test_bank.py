from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from better.domain.models import Market, Match
from better.services.bank_service import (
    BankBet,
    BankBetCancellation,
    BankInitialize,
    BankService,
    BankSettlement,
    BankSlipBet,
    BankTransfer,
)
from better.storage.repository import Repository

NOW = datetime(2026, 10, 1, 10, tzinfo=UTC)
MATCH = Match(
    match_id="fixture",
    competition="fixture-league",
    season="2026",
    kickoff=NOW + timedelta(hours=2),
    home_team="Fixture Home",
    away_team="Fixture Away",
    home_team_id="home",
    away_team_id="away",
)


def bet(amount="10", odds="2.5"):
    return BankBet(
        request_id=uuid4(),
        match_id=MATCH.match_id,
        amount=amount,
        odds=odds,
        market=Market(type="1X2", selection="HOME"),
    )


@pytest.fixture
def bank(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'bank.db'}")
    service = BankService(repo, clock=lambda: NOW)
    yield service
    repo.close()


def initialize(bank, amount="100"):
    return bank.initialize(BankInitialize(request_id=uuid4(), amount=amount))


def test_bank_has_no_invented_initial_budget(bank):
    assert not bank.status()["initialized"]
    assert bank.portfolio(NOW) is None


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "10.001"])
def test_invalid_budget_rejected(amount):
    with pytest.raises(ValidationError):
        BankInitialize(request_id=uuid4(), amount=amount)


def test_initialization_is_immutable_and_idempotent(bank):
    request = BankInitialize(request_id=uuid4(), amount="100")
    bank.initialize(request)
    bank.initialize(request)
    assert bank.status()["event_count"] == 1
    with pytest.raises(ValueError):
        initialize(bank, "200")
    with pytest.raises(ValueError):
        bank.initialize(BankInitialize(request_id=request.request_id, amount="99"))


def test_deposit_withdraw_are_idempotent_and_keep_bet_profit_separate(bank):
    initialize(bank)
    deposit = BankTransfer(request_id=uuid4(), amount="25.00")
    assert Decimal(bank.deposit(deposit)["available"]) == 125
    assert bank.deposit(deposit)["event_count"] == 2
    bank.record_bet(bet("20"), MATCH)
    with pytest.raises(ValueError, match="insufficiente"):
        bank.withdraw(BankTransfer(request_id=uuid4(), amount="106"))
    withdrawal = BankTransfer(request_id=uuid4(), amount="5.00")
    state = bank.withdraw(withdrawal)
    assert Decimal(state["available"]) == 100
    assert Decimal(state["reserved"]) == 20
    assert Decimal(state["equity"]) == 120
    assert Decimal(state["profit"]) == 0
    assert [item["kind"] for item in state["transfers"]] == ["WITHDRAWAL", "DEPOSIT"]
    assert bank.withdraw(withdrawal)["event_count"] == 4
    assert Decimal(bank.portfolio(NOW).bankroll) == 120


def test_deposit_does_not_erase_betting_drawdown(bank):
    initialize(bank)
    request = bet("100")
    bank.record_bet(request, MATCH)
    bank.clock = lambda: NOW + timedelta(hours=3)
    bank.settle(request.request_id, BankSettlement(request_id=uuid4(), result="LOST"))
    state = bank.deposit(BankTransfer(request_id=uuid4(), amount="70"))
    assert Decimal(state["available"]) == 70
    assert Decimal(state["profit"]) == -100
    assert state["drawdown"] == 1


@pytest.mark.parametrize(
    "result,available,profit", [("WON", "115.00", "15.00"), ("LOST", "90", "-10"), ("VOID", "100", "0")]
)
def test_cash_reserved_equity_and_settlement(bank, result, available, profit):
    initialize(bank)
    request = bet()
    state = bank.record_bet(request, MATCH)
    assert Decimal(state["available"]) == 90
    assert Decimal(state["reserved"]) == 10
    assert Decimal(state["equity"]) == 100
    bank.record_bet(request, MATCH)
    assert bank.status()["event_count"] == 2
    bank.clock = lambda: NOW + timedelta(hours=4)
    settlement = BankSettlement(request_id=uuid4(), result=result)
    state = bank.settle(request.request_id, settlement)
    assert Decimal(state["available"]) == Decimal(available)
    assert Decimal(state["profit"]) == Decimal(profit)
    assert Decimal(state["reserved"]) == 0
    bank.settle(request.request_id, settlement)
    with pytest.raises(ValueError):
        bank.settle(request.request_id, BankSettlement(request_id=uuid4(), result=result))
    portfolio = bank.portfolio(NOW + timedelta(hours=5))
    assert portfolio.positions[0].stake_amount == 10
    assert portfolio.positions[0].settled_at is not None
    assert portfolio.bankroll == Decimal(available)
    # Historical analysis sees only the earlier open position, never later winnings/losses.
    earlier = bank.portfolio(NOW + timedelta(minutes=30))
    assert earlier.bankroll == 100 and earlier.positions[0].settled_at is None


def test_insufficient_cash_and_early_result_are_rejected(bank):
    initialize(bank)
    with pytest.raises(ValueError, match="insufficiente"):
        bank.record_bet(bet("101"), MATCH)
    request = bet()
    bank.record_bet(request, MATCH)
    with pytest.raises(ValueError, match="prima del calcio"):
        bank.settle(request.request_id, BankSettlement(request_id=uuid4(), result="WON"))


def test_exhausted_bankroll_is_zero_and_blocks_further_stakes(bank):
    initialize(bank)
    request = bet("100")
    bank.record_bet(request, MATCH)
    bank.clock = lambda: NOW + timedelta(hours=3)
    bank.settle(request.request_id, BankSettlement(request_id=uuid4(), result="LOST"))
    portfolio = bank.portfolio(NOW + timedelta(hours=4))
    assert portfolio.bankroll == 0
    assert portfolio.drawdown == 1
    assert portfolio.daily_loss_fraction == 1


def test_concurrent_bets_cannot_overspend(bank):
    initialize(bank)

    def record(_):
        try:
            bank.record_bet(bet("60"), MATCH)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(record, range(2)))
    assert sum(outcomes) == 1
    assert Decimal(bank.status()["available"]) == 40


def test_ledger_survives_new_service_instance(bank):
    initialize(bank)
    assert BankService(bank.repository).status()["initial_budget"] == "100"


def test_manual_bet_cancellation_is_append_only_and_restores_balance(bank):
    initialize(bank)
    request = bet()
    bank.record_bet(request, MATCH)
    cancellation = BankBetCancellation(request_id=uuid4())
    state = bank.cancel_manual_bet(request.request_id, cancellation)
    assert Decimal(state["available"]) == 100
    assert Decimal(state["reserved"]) == 0
    assert state["bets"] == []
    assert state["event_count"] == 3
    assert bank.cancel_manual_bet(request.request_id, cancellation)["event_count"] == 3
    events = bank.repository.list_records("annotations", last=10, entity_id="bank:EUR:v1")
    assert {event["kind"] for event in events} == {"INITIAL", "BET", "BET_CANCELLED"}
    with pytest.raises(ValueError, match="inesistente|liquidata"):
        bank.settle(request.request_id, BankSettlement(request_id=uuid4(), result="VOID"))


def test_salabet_bet_cannot_be_cancelled_from_bank(bank):
    initialize(bank)
    placed = BankSlipBet(
        request_id=uuid4(),
        betslip_id=uuid4(),
        amount="10",
        odds="2.5",
        bookmaker="888",
        kickoff=MATCH.kickoff,
        match_id=MATCH.match_id,
        team_ids=(MATCH.home_team_id, MATCH.away_team_id),
        competition=MATCH.competition,
        market=Market(type="1X2", selection="HOME"),
    )
    bank.record_slip(placed)
    with pytest.raises(ValueError, match="SalaBet"):
        bank.cancel_manual_bet(placed.request_id, BankBetCancellation(request_id=uuid4()))
    state = bank.status()
    assert Decimal(state["available"]) == 90
    assert len(state["bets"]) == 1 and state["bets"][0]["source"] == "SALABET"


def test_cancelling_settled_manual_bet_rebuilds_balance_and_statistics(bank):
    initialize(bank)
    request = bet()
    bank.record_bet(request, MATCH)
    bank.clock = lambda: NOW + timedelta(hours=4)
    bank.settle(request.request_id, BankSettlement(request_id=uuid4(), result="WON"))
    state = bank.cancel_manual_bet(
        request.request_id, BankBetCancellation(request_id=uuid4())
    )
    assert Decimal(state["available"]) == Decimal(state["equity"]) == 100
    assert Decimal(state["profit"]) == 0
    assert Decimal(state["peak"]) == 100
    assert state["drawdown"] == 0
    assert state["bets"] == []


async def test_pipeline_uses_bank_snapshot_when_external_portfolio_absent(bank):
    from dataclasses import replace

    from better.config import Settings
    from better.domain.schemas import AnalysisRequest
    from better.providers.fake import make_demo_bundle
    from better.services.application import make_application
    from better.system_one import SystemOneSettings

    initialize(bank, "250")
    bundle = replace(make_demo_bundle("bet", as_of=NOW), portfolio=None)
    engine = make_application(
        settings=Settings(),
        providers=bundle,
        repository=bank.repository,
        system_one_settings=SystemOneSettings(enabled=False),
    )
    run = await engine.analyze(AnalysisRequest(match_id="demo-match", as_of=NOW))
    assert run.input_manifest["portfolio"]["bankroll"] == "250"
    assert run.results["risk_manager"].status == "OK"
    assert run.decision.stake.amount <= Decimal("2.50")


def test_bank_api_records_without_bookmaker_execution_and_checks_origin():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from better.api.app import create_app
    from better.config import Settings
    from better.providers.base import ProviderBundle
    from better.services.application import UnconfiguredProvider, make_application
    from better.system_one import SystemOneSettings

    match = MATCH.model_copy(update={"kickoff": datetime.now(UTC) + timedelta(days=1)})
    fixtures = SimpleNamespace(get_match=AsyncMock(return_value=match))
    missing = UnconfiguredProvider()
    repository = Repository("sqlite:///:memory:")
    engine = make_application(
        settings=Settings(),
        repository=repository,
        providers=ProviderBundle(fixtures, missing, missing, missing, missing),
        system_one_settings=SystemOneSettings(enabled=False),
    )
    with TestClient(create_app(engine)) as client:
        assert client.get("/bank").json()["initialized"] is False
        init = BankInitialize(request_id=uuid4(), amount="100").model_dump(mode="json")
        assert (
            client.post("/bank/initialize", json=init, headers={"Origin": "https://foreign.test"}).status_code
            == 403
        )
        assert client.post("/bank/initialize", json=init).status_code == 200
        transfer = BankTransfer(request_id=uuid4(), amount="10").model_dump(mode="json")
        assert client.post("/bank/deposit", json=transfer).json()["available"] == "110"
        assert client.post("/bank/deposit", json=transfer).json()["event_count"] == 2
        assert client.post("/bank/withdraw", json=BankTransfer(request_id=uuid4(), amount="111").model_dump(mode="json")).status_code == 409
        assert client.post("/bank/withdraw", json=BankTransfer(request_id=uuid4(), amount="10").model_dump(mode="json")).json()["available"] == "100"
        request = bet()
        response = client.post("/bank/bets", json=request.model_dump(mode="json"))
        assert response.status_code == 200
        assert Decimal(response.json()["available"]) == 90
        assert response.json()["automatic_execution"] is False
        removable = bet()
        assert client.post("/bank/bets", json=removable.model_dump(mode="json")).status_code == 200
        removed = client.post(
            f"/bank/bets/{removable.request_id}/remove",
            json=BankBetCancellation(request_id=uuid4()).model_dump(mode="json"),
        )
        assert removed.status_code == 200
        assert Decimal(removed.json()["available"]) == 90
        assert all(item["request_id"] != str(removable.request_id) for item in removed.json()["bets"])
        result = client.post(
            f"/bank/bets/{request.request_id}/settle",
            json=BankSettlement(request_id=uuid4(), result="VOID").model_dump(mode="json"),
        )
        assert result.status_code == 200
        assert Decimal(result.json()["available"]) == 100
    repository.close()
