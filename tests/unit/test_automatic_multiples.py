from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from better.config import Settings
from better.domain.salabet import BetCandidateCreate, BetSlipAction
from better.domain.schemas import AnalysisRequest, AnalysisRun
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.services.automatic_multiples import AutomaticMultiples, MultipleSwitch
from better.services.batch_analysis import BatchAnalysisService
from better.services.salabet_service import SalaBetService
from better.storage.repository import Repository

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture
async def setup():
    repo = Repository("sqlite:///:memory:")
    settings = Settings(min_sample=1000, timing_comparison_competitions=("SYNTHETIC_LEAGUE",))
    app = make_application(settings=settings, repository=repo, providers=make_demo_bundle("bet", as_of=NOW))
    base = await app.analyze(AnalysisRequest(match_id="demo-match", as_of=NOW))
    clock = [NOW + timedelta(seconds=2)]
    runtime = SimpleNamespace(repository=repo, settings=settings)
    service = AutomaticMultiples(runtime, clock=lambda: clock[0])

    def seed(index, hours=6):
        data = (
            base.model_dump_json()
            .replace("demo-match", f"match-{index}")
            .replace(str(base.analysis_id), str(uuid4()))
        )
        data = data.replace(base.snapshot.match.home_team_id, f"home-{index}").replace(
            base.snapshot.match.away_team_id, f"away-{index}"
        )
        run = AnalysisRun.model_validate_json(data)
        match = run.snapshot.match.model_copy(update={"kickoff": NOW + timedelta(hours=hours)})
        run = run.model_copy(update={"snapshot": run.snapshot.model_copy(update={"match": match})})
        repo.save_match(match)
        with repo.engine.begin() as conn:
            repo._append(
                conn,
                "analysis_runs",
                identifier=str(run.analysis_id),
                entity_id=str(run.analysis_id),
                match_id=run.match_id,
                payload=run.model_dump(mode="json"),
            )
        choice, _ = BatchAnalysisService(runtime)._best_candidate(run)
        candidate = SalaBetService(repo, clock=lambda: clock[0]).create_batch_candidate(
            BetCandidateCreate(
                analysis_id=run.analysis_id,
                prediction_id=choice["prediction_id"],
                market=choice["market"],
                selection=choice["selection"],
            ),
            {"batch_id": uuid4()},
        )
        return run, candidate

    yield service, repo, clock, seed
    await app.close()
    repo.close()


def prepare(service, plan=None, request_id=None):
    return service.update(
        MultipleSwitch(
            request_id=request_id or uuid4(), enabled=True, plan_id=(plan or service.proposal())["plan_id"]
        )
    )


async def test_button_selects_six_visible_matches_and_creates_ready_without_placing(setup):
    service, repo, _, seed = setup
    for i in range(8):
        seed(i, hours=3 + i)
    plan = service.proposal()
    assert len(plan["selected"]) == 6
    assert len({c["match_id"] for c in plan["selected"]}) == 6
    assert "groups" not in plan
    before = {t: repo.all_records(t) for t in ("validation_observations", "canonical_predictions")}
    bank = repo.all_records("annotations", entity_id="bank:EUR:v1")
    report = prepare(service, plan)
    slips = SalaBetService(repo).list_slips()
    assert slips and all(s.status == "READY" for s in slips)
    assert sum(s.stake for s in slips) == 50
    assert all(not x.operationally_eligible for s in slips for x in s.selections)
    assert report["allocated_eur"] == "50.00" and report["unallocated_eur"] == "0.00"
    assert all(3 <= len(s.selections) <= 6 and s.bookmaker for s in slips)
    assert all(len({x.match_id for x in s.selections}) == len(s.selections) for s in slips)
    assert all(x.odds_at_placement is not None for s in slips for x in s.selections)
    assert repo.all_records("annotations", entity_id="bank:EUR:v1") == bank
    for table, rows in before.items():
        assert repo.all_records(table) == rows
    assert not any(e.get("kind") == "BET_PLACED" for e in repo.all_records("bet_events"))


async def test_only_next_24h_visible_candidates_are_used(setup):
    service, repo, clock, seed = setup
    seed(0, hours=25)
    seed(1, hours=-1)
    for i in range(2, 6):
        seed(i, hours=12)
    candidate = next(
        c for c in SalaBetService(repo, clock=lambda: clock[0]).list_candidates() if c.match_id == "match-2"
    )
    SalaBetService(repo, clock=lambda: clock[0]).remove_candidate(
        candidate.id, BetSlipAction(request_id=uuid4())
    )
    plan = service.proposal()
    assert len(plan["selected"]) == 3
    assert all(c["match_id"] not in {"match-0", "match-1", candidate.match_id} for c in plan["selected"])


async def test_not_enough_matches_does_not_create_or_reserve_money(setup):
    service, repo, _, seed = setup
    seed(0)
    seed(1)
    with pytest.raises(ValueError, match="almeno tre"):
        prepare(service)
    assert not repo.all_records("bet_slips")
    assert service.report()["allocated_eur"] == "0.00"


async def test_polling_never_prepares_and_restart_or_request_retry_never_duplicate(setup):
    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    assert service.collect()["created"] == []
    plan = service.proposal()
    request_id = uuid4()
    prepare(service, plan, request_id)
    before = repo.all_records("bet_events")
    prepare(service, plan, request_id)
    prepare(AutomaticMultiples(service.runtime, clock=lambda: clock[0]), plan)
    assert service.collect()["created"] == []
    assert repo.all_records("bet_events") == before


async def test_changed_pool_requires_new_review_and_cannot_reset_turn_budget(setup):
    service, _, _, seed = setup
    for i in range(3):
        seed(i)
    old = service.proposal()
    seed(4)
    with pytest.raises(ValueError, match="cambiata"):
        prepare(service, old)
    prepare(service)
    seed(5)
    with pytest.raises(ValueError, match="gia assegnato"):
        prepare(service)


async def test_rollback_during_ready_keeps_no_partial_slips_or_reservations(setup, monkeypatch):
    service, repo, _, seed = setup
    for i in range(6):
        seed(i)
    original = SalaBetService.confirm_slip

    def fail(*args, **kwargs):
        raise RuntimeError("interruption")

    monkeypatch.setattr(SalaBetService, "confirm_slip", fail)
    with pytest.raises(RuntimeError):
        prepare(service)
    assert not repo.all_records("bet_slips")
    assert service.report()["allocated_eur"] == "0.00"
    monkeypatch.setattr(SalaBetService, "confirm_slip", original)
    prepare(service)
    assert service.report()["allocated_eur"] == "50.00"


async def test_concurrent_clicks_share_one_budget(setup, tmp_path):
    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    plan = service.proposal()
    target = Repository("sqlite:///" + (tmp_path / "concurrent.db").as_posix())
    a, b = repo.engine.raw_connection(), target.engine.raw_connection()
    try:
        a.driver_connection.backup(b.driver_connection)
    finally:
        a.close()
        b.close()
    runtime = SimpleNamespace(repository=target, settings=service.settings)

    def click(_):
        return prepare(AutomaticMultiples(runtime, clock=lambda: clock[0]), plan)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(click, range(2)))
        assert sum(s.stake for s in SalaBetService(target).list_slips()) == Decimal("50")
        assert (
            len(
                [
                    r
                    for r in target.all_records("annotations", entity_id="automatic-multiples:v1")
                    if r["kind"] == "PREPARED"
                ]
            )
            == 1
        )
    finally:
        target.close()


def test_alternatives_rotate_markets_and_limit_recurrence():
    choices = [
        {"match_id": str(i), "market": m, "selection": "S", "ranking_score": 100 - i, "bookmaker": "888"}
        for i in range(6)
        for m in ("1X2", "BTTS", "OVER_UNDER_2_5")
    ]
    slips = AutomaticMultiples.compose(choices)
    assert len(slips) == 3
    used, counts = set(), {}
    for slip in slips:
        for c in slip:
            key = (c["match_id"], c["market"])
            assert key not in used
            used.add(key)
            counts[c["match_id"]] = counts.get(c["match_id"], 0) + 1
    assert max(counts.values()) == 2


def test_six_primaries_without_alternatives_make_two_disjoint_triples():
    choices = [
        {"match_id": str(i), "market": "1X2", "selection": "HOME", "ranking_score": 50, "bookmaker": "888"}
        for i in range(6)
    ]
    slips = AutomaticMultiples.compose(choices)
    assert len(slips) == 2
    assert all(len(s) == 3 for s in slips)
    assert set(c["match_id"] for c in slips[0]).isdisjoint(c["match_id"] for c in slips[1])


async def test_future_sources_are_rejected_before_ranking(setup, monkeypatch):
    service, repo, _, seed = setup
    run, _ = seed(0)
    for i in range(1, 3):
        seed(i)
    get_run = repo.get_run
    quotes = list(run.snapshot.odds.quotes)
    quotes[0] = quotes[0].model_copy(update={"captured_at": NOW + timedelta(hours=1)})
    invalid = run.model_copy(
        update={
            "snapshot": run.snapshot.model_copy(
                update={"odds": run.snapshot.odds.model_copy(update={"quotes": tuple(quotes)})}
            )
        }
    )
    monkeypatch.setattr(
        repo,
        "get_run",
        lambda identifier: invalid if str(identifier) == str(run.analysis_id) else get_run(identifier),
    )
    plan = service.proposal()
    assert len(plan["selected"]) == 2
    assert any(r["reason"] == "LEAKAGE_REJECTED" for r in plan["rejected"])
    with pytest.raises(ValueError):
        prepare(service, plan)
    assert not repo.all_records("bet_slips")


async def test_existing_user_ticket_is_not_changed_or_duplicated(setup):
    from better.domain.salabet import BetSlipCreate

    service, repo, clock, seed = setup
    candidates = [seed(i)[1] for i in range(3)]
    desk = SalaBetService(repo, clock=lambda: clock[0])
    original = desk.create_slip(
        BetSlipCreate(request_id=uuid4(), candidate_ids=tuple(c.id for c in candidates))
    )
    try:
        prepare(service)
    except ValueError as error:
        assert "gia presenti" in str(error)
    assert desk.get_slip(original.id) == original
    signatures = [
        tuple(sorted((s.match_id, s.market.value, s.selection) for s in slip.selections))
        for slip in desk.list_slips()
    ]
    assert len(signatures) == len(set(signatures))


async def test_auto_test_placement_is_off_by_default_and_debits_once(setup):
    from better.services.automatic_multiples import MultipleAutoSwitch
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("100")))
    assert not service.report()["enabled"]
    assert not service.collect()["created"]
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    result = service.collect()
    assert result["created"] and not result["diagnostics"], result
    slips = SalaBetService(repo, clock=lambda: clock[0]).list_slips()
    assert all(s.status == "OPEN" for s in slips)
    assert sum(s.stake for s in slips) == Decimal("50")
    assert Decimal(bank.status()["available"]) == Decimal("50")
    assert len(bank.status()["bets"]) == len(slips)
    assert len({b["request_id"] for b in bank.status()["bets"]}) == len(slips)
    assert not service.collect()["created"]
    restarted = AutomaticMultiples(service.runtime, clock=lambda: clock[0])
    assert not restarted.collect()["created"]
    assert Decimal(bank.status()["available"]) == Decimal("50")


async def test_custom_budget_and_no_lowering_below_allocated(setup):
    from better.services.automatic_multiples import MultipleAutoSwitch, MultipleBudget
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("100")))
    command = MultipleBudget(request_id=uuid4(), budget_eur=Decimal("75"))
    report = service.update_policy(command)
    assert report["budget_eur"] == "75"
    assert service.update_policy(command)["control"]["sequence"] == report["control"]["sequence"]
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    assert service.collect()["created"]
    assert Decimal(bank.status()["available"]) == Decimal("25")
    with pytest.raises(ValueError, match="inferiore"):
        service.update_policy(MultipleBudget(request_id=uuid4(), budget_eur=Decimal("50")))


async def test_auto_insufficient_bankroll_creates_no_slips_or_movements(setup):
    from better.services.automatic_multiples import MultipleAutoSwitch
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("20")))
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    before = bank.status()
    result = service.collect()
    assert "INSUFFICIENT_BANKROLL" in result["error"]
    assert bank.status() == before
    assert not SalaBetService(repo).list_slips()
    service.collect()
    assert len(service.report()["errors"]) == 1


async def test_midway_placement_failure_rolls_back_bank_and_slips(setup, monkeypatch):
    from better.services.automatic_multiples import MultipleAutoSwitch
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    assert len(service.compose(service.proposal()["alternatives"])) > 1
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("100")))
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    original = SalaBetService.place_slip
    attempts = 0

    def failing_place(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ValueError("INVALID_ODDS: errore controllato")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SalaBetService, "place_slip", failing_place)
    result = service.collect()
    assert "INVALID_ODDS" in result["error"]
    assert attempts == 2
    assert Decimal(bank.status()["available"]) == Decimal("100")
    assert not bank.status()["bets"]
    assert not SalaBetService(repo).list_slips()


async def test_disabling_and_passed_kickoff_prevent_automatic_place(setup):
    from better.services.automatic_multiples import MultipleAutoSwitch
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(3):
        seed(i)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("100")))
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=False))
    assert not service.collect()["created"]
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    clock[0] += timedelta(hours=7)
    assert not service.collect()["created"]
    assert not bank.status()["bets"]


async def test_concurrent_automatic_cycles_debit_only_once(setup, tmp_path):
    from better.services.automatic_multiples import MultipleAutoSwitch
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    BankService(repo, clock=lambda: clock[0]).initialize(
        BankInitialize(request_id=uuid4(), amount=Decimal("100"))
    )
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True))
    target = Repository("sqlite:///" + (tmp_path / "auto-concurrent.db").as_posix())
    source, destination = repo.engine.raw_connection(), target.engine.raw_connection()
    try:
        source.driver_connection.backup(destination.driver_connection)
    finally:
        source.close()
        destination.close()
    runtime = SimpleNamespace(repository=target, settings=service.settings)
    try:

        def run(_):
            return AutomaticMultiples(runtime, clock=lambda: clock[0]).collect()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, range(2)))
        bank = BankService(target, clock=lambda: clock[0]).status()
        slips = SalaBetService(target, clock=lambda: clock[0]).list_slips()
        assert Decimal(bank["available"]) == Decimal("50")
        assert sum(s.stake for s in slips) == Decimal("50")
        assert len(bank["bets"]) == len(slips)
        assert len({b["request_id"] for b in bank["bets"]}) == len(slips)
    finally:
        target.close()


def verified_runtime(service, repo, clock):
    runs = {
        r["match_id"]: repo.get_run(r["analysis_id"])
        for ticket in service.preview()["tickets"]
        for r in ticket["selections"]
    }

    async def match(match_id):
        return runs[match_id].snapshot.match

    async def odds(match_id, now):
        return runs[match_id].snapshot.odds

    service.runtime.providers = SimpleNamespace(
        fixtures=SimpleNamespace(get_match=match), odds=SimpleNamespace(get_odds=odds)
    )
    return runs


def approve_verified(service, future=False):
    from better.services.automatic_multiples import MultipleAutoSwitch

    service.update_policy(
        MultipleAutoSwitch(
            request_id=uuid4(),
            enabled=True,
            preview_id=service.preview()["preview_id"],
            approval_mode="FUTURE_PROPOSALS" if future else "CURRENT_PROPOSAL",
        )
    )


def refresh_runtime(setup):
    service, repo, clock, seed = setup
    for index in range(3):
        seed(index)
    service.settings = service.settings.model_copy(update={'stats_ttl_seconds': 60})
    service.runtime.settings = service.settings
    runs = verified_runtime(service, repo, clock)
    calls = []

    async def analyze(request, **options):
        calls.append((request, options))
        old = runs[request.match_id]
        run = old.model_copy(update={'analysis_id': uuid4(), 'as_of': clock[0], 'created_at': clock[0]})
        with repo.engine.begin() as conn:
            repo._append(conn, 'analysis_runs', identifier=str(run.analysis_id), entity_id=str(run.analysis_id),
                         match_id=run.match_id, payload=run.model_dump(mode='json'))
        runs[request.match_id] = run
        return run

    service.runtime.analyze = analyze
    approve_verified(service, future=True)
    clock[0] = NOW + timedelta(seconds=61)
    return service, repo, clock, runs, calls


async def test_stale_candidates_refresh_once_without_money_slips_or_fake_t24(setup):
    from better.services.candidate_refresh import CandidateRefresh
    service, repo, clock, _, calls = refresh_runtime(setup)
    old_preview = service.preview()
    bank = repo.all_records('annotations', entity_id='bank:EUR:v1')
    t24 = repo.all_records('annotations', entity_id='timing-comparison:v1')
    result = await service.collect_verified()
    assert len(result['analysis_refresh']['refreshed']) == 3, result
    assert not result['created'] and len(calls) == 3
    assert all(options == {'cache_only_research': True} for _, options in calls)
    assert all(request.parent_analysis_id for request, _ in calls)
    assert repo.all_records('annotations', entity_id='bank:EUR:v1') == bank
    assert repo.all_records('annotations', entity_id='timing-comparison:v1') == t24
    assert not SalaBetService(repo).list_slips()
    visible = SalaBetService(repo, clock=lambda: clock[0]).list_candidates()
    assert len(visible) == 3 and len({c.match_id for c in visible}) == 3
    assert all(repo.get_run(c.analysis_id).as_of == clock[0] for c in visible)
    restarted = AutomaticMultiples(service.runtime, clock=lambda: clock[0])
    await CandidateRefresh(restarted).run(old_preview)
    assert len(calls) == 3


async def test_current_proposal_requires_new_approval_after_refresh(setup):
    service, _, _, _, _ = refresh_runtime(setup)
    approve_verified(service, future=False)
    assert 'ANALYSIS_REFRESHED' in (await service.collect_verified())['diagnostics'][0]
    result = await service.collect_verified()
    assert 'APPROVAL_REQUIRED' in result['diagnostics'][0]
    assert not result['created']


async def test_refresh_failure_has_bounded_retries_and_survives_restart(setup):
    service, repo, clock, _, _ = refresh_runtime(setup)
    failures = []

    async def fail(*args, **options):
        failures.append(True)
        raise RuntimeError('sensitive provider details must not be persisted')

    service.runtime.analyze = fail
    result = await service.collect_verified()
    assert len(result['analysis_refresh']['failed']) == 3
    await service.collect_verified()
    assert len(failures) == 3
    restarted = AutomaticMultiples(service.runtime, clock=lambda: clock[0])
    await restarted.collect_verified()
    assert len(failures) == 3
    clock[0] += timedelta(minutes=15)
    await restarted.collect_verified()
    assert len(failures) == 6
    await restarted.collect_verified()
    assert restarted.execution_status()['consecutive_errors'] == 2
    clock[0] += timedelta(minutes=15)
    await restarted.collect_verified()
    assert len(failures) == 9
    assert restarted.execution_status()['paused'] is True
    await restarted.collect_verified()
    assert len(failures) == 9
    assert all('sensitive' not in str(r) for r in repo.all_records('annotations', entity_id='candidate-refresh:v1'))


async def test_refresh_never_runs_after_kickoff_or_accepts_future_evidence(setup):
    service, repo, clock, runs, calls = refresh_runtime(setup)
    from better.services.candidate_refresh import CandidateRefresh
    preview = service.preview()
    original = service.runtime.analyze

    async def future_evidence(request, **options):
        run = await original(request, **options)
        snapshot = run.snapshot
        stats = snapshot.stats.model_copy(update={'captured_at': clock[0] + timedelta(seconds=1)})
        return run.model_copy(update={'snapshot': snapshot.model_copy(update={'stats': stats})})

    service.runtime.analyze = future_evidence
    report = await CandidateRefresh(service).run(preview)
    assert len(report['failed']) == 3
    assert not report['refreshed']
    assert len(SalaBetService(repo, clock=lambda: clock[0]).list_candidates()) == 3
    clock[0] = next(iter(runs.values())).snapshot.match.kickoff
    await CandidateRefresh(service).run(preview)
    assert len(calls) == 3


async def test_refresh_reuses_a_newer_local_analysis_without_another_research(setup):
    service, repo, clock, runs, calls = refresh_runtime(setup)
    for match_id in list(runs):
        await service.runtime.analyze(AnalysisRequest(match_id=match_id))
    calls.clear()
    result = await service.collect_verified()
    assert len(result['analysis_refresh']['refreshed']) == 3
    assert not calls


async def test_stale_precheck_runs_before_final_quote_checks(setup):
    service, _, _, _, _ = refresh_runtime(setup)
    checked = []

    async def quote_failure(preview):
        checked.append(True)
        raise AssertionError('old quote checks must not prevent stale analysis refresh')

    service.final_check = quote_failure
    result = await service.collect_verified()
    assert len(result['analysis_refresh']['refreshed']) == 3
    assert not checked


async def test_disabling_automation_stops_remaining_refresh_jobs(setup):
    from better.services.automatic_multiples import MultipleAutoSwitch
    service, _, _, _, calls = refresh_runtime(setup)
    original = service.runtime.analyze

    async def stop_after_first(request, **options):
        result = await original(request, **options)
        service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=False))
        return result

    service.runtime.analyze = stop_after_first
    result = await service.collect_verified()
    assert len(calls) == 1
    assert len(result['analysis_refresh']['refreshed']) == 1
    assert not result['created']
    assert result['enabled'] is False


async def test_parallel_refresh_claims_do_not_duplicate_analyses_or_candidates(setup):
    import asyncio
    service, repo, clock, _, calls = refresh_runtime(setup)
    original = service.runtime.analyze
    gate = asyncio.Event()

    async def held(request, **options):
        await gate.wait()
        return await original(request, **options)

    service.runtime.analyze = held
    restarted = AutomaticMultiples(service.runtime, clock=lambda: clock[0])
    first = asyncio.create_task(service.collect_verified())
    second = asyncio.create_task(restarted.collect_verified())
    await asyncio.sleep(.01)
    gate.set()
    await asyncio.gather(first, second)
    assert len(calls) == 3
    assert len(SalaBetService(repo, clock=lambda: clock[0]).list_candidates()) == 3
    assert not SalaBetService(repo).list_slips()


async def test_refresh_uses_existing_budget_guard_and_never_changes_prepared_slips(setup, monkeypatch):
    from better.domain.salabet import BetSlipCreate
    from better.services.automation_control import AutomationControl, AutomationSwitch, ResearchLimits
    monkeypatch.setenv('OPENAI_COST_USD_EUR_RATE', '0.9')
    service, repo, clock, _, calls = refresh_runtime(setup)
    desk = SalaBetService(repo, clock=lambda: clock[0])
    candidate = desk.list_candidates()[0]
    desk.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(candidate.id,)))
    slips = repo.all_records('bet_slips')
    control = AutomationControl(repo, service.settings, clock=lambda: clock[0])
    control.update(ResearchLimits(request_id=uuid4(), per_match_eur='0', per_day_eur='0', per_week_eur='0'))
    control.update(AutomationSwitch(request_id=uuid4(), enabled=True))
    policy = control.policy()
    original = service.runtime.analyze
    reservations = []

    async def guarded(request, **options):
        guard = options['research_budget']
        reservations.append(guard.reserve('external', '.01'))
        return await original(request, **options)

    service.runtime.analyze = guarded
    result = await service.collect_verified()
    assert len(result['analysis_refresh']['refreshed']) == 3
    assert reservations == [None, None, None]
    assert all('research_budget' in options for _, options in calls)
    assert control.policy() == policy
    assert repo.all_records('bet_slips') == slips
    assert not repo.all_records('annotations', entity_id='bank:EUR:v1')


async def scheduled_verify(service):
    result = await service.collect_verified()
    if result.get("prepared"):
        return await service.collect_verified()
    return result


async def test_preview_is_read_only_and_exact_approval_places_only_shown_tickets(setup):
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(6):
        seed(i, hours=.25)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("100")))
    verified_runtime(service, repo, clock)
    before = bank.status()
    preview = service.preview()
    assert sum(Decimal(t["amount_eur"]) for t in preview["tickets"]) == Decimal("50")
    assert Decimal(preview["bank_after_eur"]) == Decimal("50")
    assert bank.status() == before and not SalaBetService(repo).list_slips()
    approve_verified(service)
    result = await scheduled_verify(service)
    assert result["created"], result
    assert not service.policy()["enabled"]
    assert Decimal(bank.status()["available"]) == Decimal("50")
    assert not (await scheduled_verify(service))["created"]


async def test_changed_quote_blocks_without_financial_writes_or_technical_error(setup):
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(3):
        seed(i, hours=.25)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal("100")))
    runs = verified_runtime(service, repo, clock)
    approve_verified(service)

    async def changed(match_id, now):
        snapshot = runs[match_id].snapshot.odds
        return snapshot.model_copy(
            update={
                "quotes": tuple(q.model_copy(update={"odds": float(q.odds) + 0.1}) for q in snapshot.quotes)
            }
        )

    service.runtime.providers.odds.get_odds = changed
    result = await scheduled_verify(service)
    assert "QUOTE_CHANGED" in result["diagnostics"][0]
    assert service.execution_status()["consecutive_errors"] == 0
    assert all(s.status == "READY" for s in SalaBetService(repo).list_slips()) and not bank.status()["bets"]


async def test_expired_quotes_and_newer_analysis_block_approval(setup):
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(3):
        seed(i, hours=.25)
    BankService(repo, clock=lambda: clock[0]).initialize(
        BankInitialize(request_id=uuid4(), amount=Decimal("100"))
    )
    runs = verified_runtime(service, repo, clock)
    approve_verified(service)
    await service.collect_verified()  # Persist the approved READY ticket first.
    service.settings = service.settings.model_copy(update={"odds_ttl_seconds": 3})
    clock[0] += timedelta(seconds=4)
    result = await scheduled_verify(service)
    assert "QUOTE_STALE" in result["diagnostics"][0]
    clock[0] = NOW + timedelta(seconds=10)
    old = next(iter(runs.values()))
    updated = old.model_copy(
        update={
            "analysis_id": uuid4(),
            "as_of": NOW + timedelta(seconds=3),
            "created_at": NOW + timedelta(seconds=4),
        }
    )
    with repo.engine.begin() as conn:
        repo._append(
            conn,
            "analysis_runs",
            identifier=str(updated.analysis_id),
            entity_id=str(updated.analysis_id),
            match_id=updated.match_id,
            payload=updated.model_dump(mode="json"),
        )
    result = await scheduled_verify(service)
    assert "ANALYSIS_CHANGED" in result["diagnostics"][0]
    assert all(s.status == "READY" for s in SalaBetService(repo).list_slips())


async def test_three_provider_errors_pause_and_budget_change_does_not_rearm(setup):
    from better.services.automatic_multiples import MultipleBudget
    from better.services.bank_service import BankInitialize, BankService

    service, repo, clock, seed = setup
    for i in range(3):
        seed(i, hours=.25)
    BankService(repo, clock=lambda: clock[0]).initialize(
        BankInitialize(request_id=uuid4(), amount=Decimal("100"))
    )
    verified_runtime(service, repo, clock)
    approve_verified(service, future=True)

    async def unavailable(match_id):
        raise ValueError("secret-provider-detail-must-not-be-logged")

    service.runtime.providers.fixtures.get_match = unavailable
    for _ in range(3):
        await scheduled_verify(service)
    assert service.report()["paused"]
    assert service.execution_status()["consecutive_errors"] == 3
    assert "secret-provider-detail" not in str(service.report())
    service.update_policy(MultipleBudget(request_id=uuid4(), budget_eur=Decimal("60")))
    assert service.report()["paused"]
    assert not (await scheduled_verify(service))["created"]
    approve_verified(service, future=True)
    assert not service.report()["paused"]
    assert service.execution_status()["consecutive_errors"] == 0
    assert all(s.status == "READY" for s in SalaBetService(repo).list_slips())


async def test_no_future_authorization_without_matching_preview(setup):
    from better.services.automatic_multiples import MultipleAutoSwitch

    service, repo, clock, seed = setup
    for i in range(3):
        seed(i, hours=.25)
    verified_runtime(service, repo, clock)
    with pytest.raises(ValueError, match="PREVIEW_CHANGED"):
        service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=True, preview_id="old-preview"))
    assert not service.policy()["enabled"]

async def test_future_authorization_does_not_expand_when_budget_is_increased(setup):
    from better.services.automatic_multiples import MultipleBudget
    from better.services.bank_service import BankInitialize, BankService
    service, repo, clock, seed = setup
    for i in range(3):
        seed(i, hours=.25)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal('100')))
    verified_runtime(service, repo, clock)
    approve_verified(service, future=True)
    assert service.policy()['approved_budget_eur'] == '50.00'
    service.update_policy(MultipleBudget(request_id=uuid4(), budget_eur=Decimal('75')))
    result = await scheduled_verify(service)
    assert 'APPROVAL_REQUIRED' in result['diagnostics'][0]
    assert service.report()['approval_required']
    assert Decimal(bank.status()['available']) == Decimal('100')
    assert not bank.status()['bets']

async def test_fresh_quotes_acquired_during_final_request_are_accepted(setup):
    from better.services.bank_service import BankInitialize, BankService
    service, repo, clock, seed = setup
    for i in range(3):
        seed(i, hours=.25)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal('100')))
    runs = verified_runtime(service, repo, clock)
    approve_verified(service)
    async def fresh(match_id, as_of):
        clock[0] += timedelta(seconds=1)
        snapshot = runs[match_id].snapshot.odds
        return snapshot.model_copy(update={'quotes': tuple(q.model_copy(update={'captured_at': clock[0]}) for q in snapshot.quotes)})
    service.runtime.providers.odds.get_odds = fresh
    result = await scheduled_verify(service)
    assert result['created'], result
    assert Decimal(bank.status()['available']) == Decimal('50')


def scheduled_setup(setup, hours=(6, 9, 12), *, future=True):
    from better.services.bank_service import BankInitialize, BankService
    service, repo, clock, seed = setup
    for i, offset in enumerate(hours):
        seed(i, hours=offset)
    bank = BankService(repo, clock=lambda: clock[0])
    bank.initialize(BankInitialize(request_id=uuid4(), amount=Decimal('100')))
    runs = verified_runtime(service, repo, clock)

    async def fresh_odds(match_id, now):
        snapshot = runs[match_id].snapshot.odds
        return snapshot.model_copy(update={'quotes': tuple(
            q.model_copy(update={'captured_at': now}) for q in snapshot.quotes)})

    service.runtime.providers.odds.get_odds = fresh_odds
    approve_verified(service, future=future)
    return service, repo, clock, bank


async def test_scheduled_ready_before_first_kickoff_then_open_at_t15_once_after_restart(setup):
    service, repo, clock, bank = scheduled_setup(setup)
    result = await service.collect_verified()
    assert result['prepared'] and not result['created']
    slips = SalaBetService(repo).list_slips()
    assert all(s.status == 'READY' for s in slips)
    assert Decimal(bank.status()['available']) == 100
    schedule = service.report()['scheduled_confirmations']
    assert all(r['confirm_at'] == (min(s.kickoff for s in slips[0].selections) - timedelta(minutes=15)).isoformat()
               for r in schedule)
    clock[0] = NOW + timedelta(hours=5, minutes=44, seconds=59)
    restarted = AutomaticMultiples(service.runtime, clock=lambda: clock[0])
    assert not (await restarted.collect_verified())['created']
    assert not bank.status()['bets']
    clock[0] += timedelta(seconds=1)
    placed = await restarted.collect_verified()
    assert placed['created']
    assert all(s.status == 'OPEN' for s in SalaBetService(repo).list_slips())
    assert Decimal(bank.status()['available']) == 50
    assert not (await restarted.collect_verified())['created']
    assert Decimal(bank.status()['available']) == 50


async def test_each_multiple_has_its_own_earliest_kickoff(setup):
    from better.services.scheduled_multiples import ScheduledMultiples
    service, repo, clock, bank = scheduled_setup(setup, hours=(3, 4, 5, 8, 9, 10))
    await service.collect_verified()
    rows = sorted(ScheduledMultiples(service).pending(), key=lambda r: r['confirm_at'])
    assert len(rows) > 1 and rows[0]['confirm_at'] != rows[-1]['confirm_at']
    spent = Decimal('0')
    for due in sorted({r['confirm_at'] for r in rows}):
        clock[0] = datetime.fromisoformat(due)
        group = [r for r in rows if r['confirm_at'] == due]
        assert set((await service.collect_verified())['created']) == {r['slip_id'] for r in group}
        spent += sum(Decimal(r['ticket']['amount_eur']) for r in group)
        assert Decimal(bank.status()['available']) == 100 - spent
    assert Decimal(bank.status()['available']) == 50


async def test_t15_missed_or_kickoff_passed_never_debits_retroactively(setup):
    service, repo, clock, bank = scheduled_setup(setup)
    await service.collect_verified()
    clock[0] = NOW + timedelta(hours=7)
    result = await service.collect_verified()
    assert 'T15_MISSED' in str(result['diagnostics'])
    assert not result['created'] and not bank.status()['bets']
    assert all(s.status == 'READY' for s in SalaBetService(repo).list_slips())


async def test_disable_or_manual_edit_prevents_scheduled_debit(setup):
    from better.domain.salabet import BetSlipUpdate
    from better.services.automatic_multiples import MultipleAutoSwitch
    service, repo, clock, bank = scheduled_setup(setup)
    await service.collect_verified()
    slip = SalaBetService(repo).list_slips()[0]
    SalaBetService(repo, clock=lambda: clock[0]).update_slip(slip.id,
        BetSlipUpdate(request_id=uuid4(), stake=Decimal('49')))
    clock[0] = NOW + timedelta(hours=5, minutes=45)
    result = await service.collect_verified()
    assert 'SCHEDULED_SLIP_CHANGED' in str(result['diagnostics'])
    assert not bank.status()['bets']
    service.update_policy(MultipleAutoSwitch(request_id=uuid4(), enabled=False))
    assert not (await service.collect_verified())['created']
    assert Decimal(bank.status()['available']) == 100


async def test_timer_wakes_for_t15_before_regular_30_minute_cycle_without_network(setup):
    from better.services.data_pipeline import DataPipelineCollector, DataPipelineService
    service, repo, clock, _ = scheduled_setup(setup, hours=(.5, 3, 6))
    await service.collect_verified()
    collector = DataPipelineCollector(service.runtime, clock=lambda: clock[0])
    regular = clock[0] + timedelta(minutes=30)
    assert collector.next_cycle_delay(regular) == 898
    assert DataPipelineService(repo).poll_seconds == 1800
    clock[0] = NOW + timedelta(minutes=15)
    assert collector.next_cycle_delay(regular) == 0
    collector.started_at = collector.completed_at = clock[0]
    assert collector.next_cycle_delay(regular) > 0


async def test_concurrent_t15_confirmation_has_one_debit_per_slip(setup):
    import asyncio
    service, repo, clock, bank = scheduled_setup(setup)
    await service.collect_verified()
    clock[0] = NOW + timedelta(hours=5, minutes=45)
    restarted = AutomaticMultiples(service.runtime, clock=lambda: clock[0])
    await asyncio.gather(service.collect_verified(), restarted.collect_verified())
    slips = SalaBetService(repo).list_slips()
    assert all(s.status == 'OPEN' for s in slips)
    assert len(bank.status()['bets']) == len(slips)
    assert Decimal(bank.status()['available']) == 50


async def test_timer_preserves_t24_window_with_30_minute_polling(setup):
    from better.services.automation_control import AutomationControl, AutomationSwitch
    from better.services.data_pipeline import DataPipelineCollector
    service, repo, clock, seed = setup
    seed(0, hours=24.25)
    AutomationControl(repo, service.settings, clock=lambda: clock[0]).update(
        AutomationSwitch(request_id=uuid4(), enabled=True))
    collector = DataPipelineCollector(service.runtime, clock=lambda: clock[0])
    regular = clock[0] + timedelta(minutes=30)
    assert collector.next_cycle_delay(regular) == 898


async def test_due_confirmation_precedes_catalog_and_archive_work(setup, monkeypatch):
    from better.services.data_pipeline import DataPipelineCollector
    from better.services.validation_cohort import ValidationCohortService
    service, _, clock, _ = scheduled_setup(setup)
    await service.collect_verified()
    clock[0] = NOW + timedelta(hours=5, minutes=45)
    calls = []

    async def confirm(self):
        calls.append('confirmation')
        return {'created': [], 'diagnostics': []}

    async def enroll(self, now):
        calls.append('catalog')
        return {'discovered': 0, 'enrolled': 0, 'excluded': 0}

    monkeypatch.setattr(AutomaticMultiples, 'collect_verified', confirm)
    monkeypatch.setattr(ValidationCohortService, 'auto_enroll', enroll)
    collector = DataPipelineCollector(service.runtime, clock=lambda: clock[0])
    with pytest.raises(AttributeError, match='stats'):
        await collector._collect_due()
    assert calls == ['confirmation', 'catalog']
