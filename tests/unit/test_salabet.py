from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from better.domain.salabet import (
    BetCandidate,
    BetSelectionVoid,
    BetSlipAction,
    BetSlipCreate,
    BetSlipUpdate,
)
from better.services.bank_service import BankInitialize
from better.services.salabet_service import SalaBetService
from better.storage.repository import Repository

NOW = datetime(2026, 10, 3, 10, tzinfo=UTC)


@pytest.fixture
def desk():
    repository = Repository("sqlite:///:memory:")
    current = [NOW]
    service = SalaBetService(repository, clock=lambda: current[0])
    service.bank.initialize(BankInitialize(request_id=uuid4(), amount="100"))
    yield service, repository, current
    repository.close()


def candidate(repository, number, *, match_id=None, market="1X2", selection="HOME", validated=True):
    identifier = uuid4()
    value = BetCandidate(
        id=identifier,
        analysis_id=uuid4(),
        prediction_id=uuid4(),
        match_id=match_id or f"match-{number}",
        competition_id="serie-c-a",
        kickoff=NOW + timedelta(hours=2 + number),
        home_team=f"Home {number}",
        away_team=f"Away {number}",
        team_ids=(f"home-{number}", f"away-{number}"),
        market=market,
        selection=selection,
        probability=0.5,
        fair_odds=2,
        odds_at_analysis=1.9,
        edge=0.02,
        ev=0.03,
        bookmaker_at_analysis="Analysis Book",
        model_name="poisson",
        model_version="1.0",
        prediction_status="VALIDATED" if validated else "INDICATIVE",
        validation_status="NORMAL_WEIGHT" if validated else "INSUFFICIENT_SAMPLE",
        validation_sample=200 if validated else 12,
        validation_required=200,
        input_completeness=0.9,
        operationally_eligible=validated,
        created_at=NOW,
        provenance={"source": "test-analysis"},
    )
    with repository.engine.begin() as connection:
        repository._append(
            connection,
            "bet_candidates",
            identifier=str(identifier),
            entity_id=str(value.analysis_id),
            match_id=value.match_id,
            payload=value.model_dump(mode="json"),
        )
    return value


def test_removed_candidate_can_be_restored_without_recreating_analysis(desk):
    service, repository, _ = desk
    item = candidate(repository, 1, validated=False)
    service.remove_candidate(item.id, BetSlipAction(request_id=uuid4()))
    assert service.list_candidates() == []
    restored = service.restore_candidate(item.id, BetSlipAction(request_id=uuid4()))
    assert restored.id == item.id and not restored.removed
    assert [row.id for row in service.list_candidates()] == [item.id]
    slip = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(item.id,)))
    assert slip.selections[0].bet_candidate_id == item.id
    service.remove_candidate(item.id, BetSlipAction(request_id=uuid4()))
    assert service.list_candidates() == []


def test_past_kickoff_candidate_leaves_da_valutare_without_deletion(desk):
    service, repository, current = desk
    item = candidate(repository, 1)
    assert [row.id for row in service.list_candidates()] == [item.id]
    current[0] = item.kickoff
    assert service.list_candidates() == []
    assert [row.id for row in service.list_candidates(include_removed=True)] == [item.id]


def prepare(service, candidates, *, stake="10", odds="2", bookmaker="Manual Book"):
    created = service.create_slip(
        BetSlipCreate(request_id=uuid4(), candidate_ids=tuple(item.id for item in candidates))
    )
    updated = service.update_slip(
        created.id,
        BetSlipUpdate(
            request_id=uuid4(),
            bookmaker=bookmaker,
            stake=stake,
            odds_at_placement={item.id: odds for item in candidates},
            note="Manual placement evidence",
        ),
    )
    return service.confirm_slip(updated.id, BetSlipAction(request_id=uuid4()))


def verified_result(repository, match_id, home, away):
    document = {
        "match_id": match_id,
        "home_score": home,
        "away_score": away,
        "status": "FINAL",
        "source_tier": "A",
        "source_url": "https://official.example/result",
        "provider": "official-test",
        "validation_eligible": True,
    }
    with repository.engine.begin() as connection:
        repository._append(
            connection,
            "match_results",
            identifier=f"result:{match_id}",
            entity_id=match_id,
            match_id=match_id,
            payload=document,
        )


def test_candidate_states_and_single_creation_are_linked(desk):
    service, repository, _ = desk
    validated = candidate(repository, 1)
    indicative = candidate(repository, 2, validated=False)
    assert {item.analysis_id for item in service.list_candidates()} == {
        validated.analysis_id,
        indicative.analysis_id,
    }
    slip = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(validated.id,)))
    assert slip.type == "SINGLE"
    assert slip.bookmaker == validated.bookmaker_at_analysis
    assert slip.selections[0].odds_at_placement == validated.odds_at_analysis
    assert slip.selections[0].analysis_id == validated.analysis_id
    assert slip.selections[0].prediction_id == validated.prediction_id
    assert not service._candidate(indicative.id).operationally_eligible


def test_multiple_limits_duplicates_incompatibility_and_correlation(desk):
    service, repository, _ = desk
    rows = [candidate(repository, index) for index in range(1, 8)]
    two = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(rows[0].id, rows[1].id)))
    six = service.create_slip(
        BetSlipCreate(request_id=uuid4(), candidate_ids=tuple(item.id for item in rows[:6]))
    )
    assert two.type == "MULTIPLE" and len(six.selections) == 6
    with pytest.raises(ValidationError):
        BetSlipCreate(request_id=uuid4(), candidate_ids=tuple(item.id for item in rows))
    with pytest.raises(ValueError, match="duplicate"):
        service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(rows[0].id, rows[0].id)))

    home = candidate(repository, 8, match_id="same", selection="HOME")
    away = candidate(repository, 9, match_id="same", selection="AWAY")
    with pytest.raises(ValueError, match="mutually exclusive"):
        service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(home.id, away.id)))
    btts = candidate(repository, 10, match_id="same", market="BTTS", selection="YES")
    correlated = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(home.id, btts.id)))
    assert correlated.warnings == ("CORRELATED_SELECTIONS_WARNING",)


def test_pre_place_updates_overspend_place_and_post_place_immutability(desk):
    service, repository, _ = desk
    row = candidate(repository, 1)
    slip = prepare(service, [row], stake="10", odds="2.25")
    assert slip.combined_odds == 2.25 and slip.potential_return == Decimal("22.50")
    placed = service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    assert placed.status == "OPEN"
    bank = service.bank.status()
    assert len(bank["bets"]) == 1 and Decimal(bank["reserved"]) == 10
    with pytest.raises(ValueError, match="immutable"):
        service.update_slip(placed.id, BetSlipUpdate(request_id=uuid4(), stake="5"))

    expensive = candidate(repository, 2)
    expensive_slip = prepare(service, [expensive], stake="100", odds="2")
    with pytest.raises(ValueError, match="INSUFFICIENT_BANKROLL"):
        service.place_slip(expensive_slip.id, BetSlipAction(request_id=uuid4()))
    assert len(service.bank.status()["bets"]) == 1


def test_prepare_is_prematch_only_and_never_moves_bank(desk):
    service, repository, current = desk
    row = candidate(repository, 1)
    created = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(row.id,)))
    assert len(service.bank.status()["bets"]) == 0
    updated = service.update_slip(
        created.id,
        BetSlipUpdate(request_id=uuid4(), bookmaker="888", stake="10"),
    )
    ready = service.confirm_slip(updated.id, BetSlipAction(request_id=uuid4()))
    assert ready.status == "READY" and len(service.bank.status()["bets"]) == 0

    late = candidate(repository, 2)
    late_slip = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(late.id,)))
    late_slip = service.update_slip(
        late_slip.id,
        BetSlipUpdate(request_id=uuid4(), bookmaker="888", stake="10"),
    )
    current[0] = late.kickoff
    with pytest.raises(ValueError, match="MATCH_ALREADY_STARTED"):
        service.confirm_slip(late_slip.id, BetSlipAction(request_id=uuid4()))
    assert len(service.bank.status()["bets"]) == 0


def test_place_is_idempotent_and_multiple_creates_one_bank_movement(desk):
    service, repository, _ = desk
    one = candidate(repository, 1)
    two = candidate(repository, 2)
    slip = prepare(service, [one, two], stake="12", odds="2")
    assert len(service.bank.status()["bets"]) == 0
    first = service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    second = service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    assert first.status == second.status == "OPEN"
    bets = service.bank.status()["bets"]
    assert len(bets) == 1 and Decimal(bets[0]["amount"]) == 12


def test_six_leg_combined_odds_keep_precision_when_recorded_in_bank(desk):
    service, repository, _ = desk
    rows = [candidate(repository, index) for index in range(1, 7)]
    slip = service.create_slip(
        BetSlipCreate(request_id=uuid4(), candidate_ids=tuple(row.id for row in rows))
    )
    odds = ["1.71", "2.32", "1.13", "1.6", "2.43", "1.88"]
    slip = service.update_slip(
        slip.id,
        BetSlipUpdate(
            request_id=uuid4(),
            bookmaker="888sport.it",
            stake="10.00",
            odds_at_placement={row.id: price for row, price in zip(rows, odds, strict=True)},
        ),
    )
    assert len(str(slip.combined_odds).split(".")[1]) > 6
    service.confirm_slip(slip.id, BetSlipAction(request_id=uuid4()))
    placed = service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    assert placed.status == "OPEN"
    bets = service.bank.status()["bets"]
    assert len(bets) == 1
    assert Decimal(bets[0]["odds"]) == Decimal(str(slip.combined_odds))
    assert service.bank.status()["available"] == "90.00"


def test_archive_hide_restore_is_append_only_and_match_state_is_exposed(desk):
    service, repository, current = desk
    row = candidate(repository, 1)
    slip = service.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(row.id,)))
    assert service.slip_view(slip)["selections"][0]["match_status"] == "NON_INIZIATA"
    service.hide_slip(slip.id, BetSlipAction(request_id=uuid4()))
    assert service.archive() == []
    assert service.list_visible_slips() == []
    assert service.get_slip(slip.id).id == slip.id
    hidden = service.archive_records(include_hidden=True)
    assert hidden[0]["hidden"] is True
    assert len(repository.list_records("bet_slips")) == 1
    assert any(row["kind"] == "HIDDEN" for row in repository.list_records("bet_events"))
    service.restore_slip(slip.id, BetSlipAction(request_id=uuid4()))
    assert service.archive()[0]["hidden"] is False
    assert len(service.list_visible_slips()) == 1
    current[0] = row.kickoff
    assert service.slip_view(service.get_slip(slip.id))["selections"][0]["match_status"] == "IN_CORSO"
    current[0] = row.kickoff + timedelta(hours=4)
    pending = service.slip_view(service.get_slip(slip.id))["selections"][0]
    assert pending["match_status"] == "RISULTATO_IN_ATTESA"
    assert pending["final_score"] is None
    verified_result(repository, row.match_id, 1, 0)
    finished = service.slip_view(service.get_slip(slip.id))["selections"][0]
    assert finished["match_status"] == "CONCLUSA"
    assert finished["final_score"] == {
        "home": 1,
        "away": 0,
        "source": "official-test",
        "source_tier": "A",
    }


@pytest.mark.parametrize(
    "selection,score,expected,returned",
    [("HOME", (2, 1), "WON", "20.00"), ("AWAY", (2, 1), "LOST", "0.00")],
)
def test_verified_single_settlement_updates_bank_archive_and_stats(
    desk, selection, score, expected, returned
):
    service, repository, current = desk
    row = candidate(repository, 1, selection=selection)
    slip = prepare(service, [row])
    service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    verified_result(repository, row.match_id, *score)
    current[0] = row.kickoff + timedelta(hours=2)
    first = service.settle_verified()
    second = service.settle_verified()
    assert first[0]["slip_status"] == expected and second == []
    bank_bet = service.bank.status()["bets"][0]
    assert bank_bet["return"] == returned
    archive = service.archive()[0]
    assert archive["analysis_id"] if "analysis_id" in archive else archive["selections"][0]["analysis_id"]
    assert service.stats()["total_bets"] == 1


def test_single_void_refunds_stake(desk):
    service, repository, _ = desk
    row = candidate(repository, 1)
    slip = prepare(service, [row])
    service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    result = service.void_selection(
        slip.id,
        row.id,
        BetSelectionVoid(request_id=uuid4(), note="Official cancellation"),
    )
    assert result.status == "VOID"
    assert service.bank.status()["bets"][0]["return"] == "10.00"


def test_multiple_void_recalculates_at_one_and_lost_leg_loses(desk):
    service, repository, current = desk
    won = candidate(repository, 1, selection="HOME")
    void = candidate(repository, 2, selection="HOME")
    slip = prepare(service, [won, void], odds="2")
    service.place_slip(slip.id, BetSlipAction(request_id=uuid4()))
    verified_result(repository, won.match_id, 1, 0)
    current[0] = won.kickoff + timedelta(hours=2)
    service.settle_verified()
    assert service.get_slip(slip.id).status == "OPEN"
    result = service.void_selection(
        slip.id,
        void.id,
        BetSelectionVoid(request_id=uuid4(), note="Leg annulled"),
    )
    assert result.status == "PARTIAL_VOID"
    assert service.bank.status()["bets"][0]["return"] == "20.00"

    lost = candidate(repository, 13, selection="AWAY")
    other = candidate(repository, 14, selection="HOME")
    second = prepare(service, [lost, other], stake="5", odds="2")
    service.place_slip(second.id, BetSlipAction(request_id=uuid4()))
    verified_result(repository, lost.match_id, 2, 0)
    verified_result(repository, other.match_id, 1, 0)
    current[0] = other.kickoff + timedelta(hours=2)
    service.settle_verified()
    assert service.get_slip(second.id).status == "LOST"


def test_slip_and_candidate_requests_are_idempotent(desk):
    service, repository, current = desk
    row = candidate(repository, 1)
    request = BetSlipCreate(request_id=uuid4(), candidate_ids=(row.id,))
    first = service.create_slip(request)
    current[0] += timedelta(minutes=1)
    second = service.create_slip(request)
    assert first.id == second.id
    assert len(repository.list_records("bet_slips")) == 1
