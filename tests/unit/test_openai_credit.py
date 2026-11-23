import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from better.api.settings import mount_settings
from better.services.api_control import APIControlService
from better.services.openai_credit import COSTS, CREDIT, CreditReference, OpenAICredit
from better.services.openai_exports import OpenAIExports, merge_exports, validate_export
from better.storage.repository import Repository

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def costs(day, amount):
    start = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())
    return {"data": [{"start_time": start, "end_time": start+86400,
        "results": [{"amount": {"value": amount, "currency": "usd"}}]}]}


@pytest.fixture
def repo():
    r = Repository("sqlite:///:memory:")
    yield r
    r.close()


def test_credit_does_not_subtract_historic_spending_twice_and_recharge_reanchors(repo):
    ledger = OpenAICredit(repo, clock=lambda: NOW)
    ledger.record_costs(costs("2026-11-09", "0.25"), source="LOCAL_EXPORT", acquired_at=NOW.isoformat())
    ledger.record_costs(costs("2026-11-07", "2.25"), source="LOCAL_EXPORT", acquired_at=NOW.isoformat())
    request = CreditReference(request_id=uuid4(), amount_usd="7.50")
    ledger.set_reference(request)
    ledger.set_reference(request)
    assert ledger.report()["amount"] == "7.50"
    assert len(repo.all_records("annotations", entity_id=CREDIT)) == 1
    ledger.record_costs(costs("2026-11-09", "0.75"), source="OPENAI_LIVE", acquired_at=(NOW+timedelta(hours=1)).isoformat())
    ledger.record_costs(costs("2026-11-10", "1.00"), source="OPENAI_LIVE", acquired_at=(NOW+timedelta(days=1)).isoformat())
    assert Decimal(ledger.report()["amount"]) == Decimal("6.00")
    # The reference day's previously billed 0.25 is not debited again.
    ledger.clock = lambda: NOW+timedelta(days=1, hours=2)
    ledger.set_reference(CreditReference(request_id=uuid4(), amount_usd="16.00"))
    assert Decimal(ledger.report()["amount"]) == Decimal("16.00")
    assert not repo.all_records("bet_events")


def test_official_and_local_costs_are_not_summed_twice_and_alerts_are_honest(repo):
    ledger = OpenAICredit(repo, clock=lambda: NOW)
    ledger.set_reference(CreditReference(request_id=uuid4(), amount_usd="7.50"))
    ledger.record_costs(costs("2026-11-09", "3.00"), source="OPENAI_LIVE", acquired_at=(NOW+timedelta(hours=1)).isoformat())
    call = {"completed_at": (NOW+timedelta(minutes=2)).isoformat(), "session_id": "s1", "cost_usd": "3.00"}
    result = ledger.report([call, call])
    assert Decimal(result["amount"]) == Decimal("4.50") and result["level"] == "LOW"
    unknown = {**call, "session_id": "s2", "cost_usd": None}
    assert ledger.report([unknown])["unknown_cost_calls"] == 1
    ledger.record_costs(costs("2026-11-09", "6.00"), source="OPENAI_LIVE", acquired_at=(NOW+timedelta(hours=2)).isoformat())
    assert ledger.report()["level"] == "CRITICAL"
    assert ledger.report([unknown])["level"] == "CRITICAL"


def test_old_export_cannot_overwrite_new_costs_and_identical_refresh_is_idempotent(repo):
    ledger = OpenAICredit(repo, clock=lambda: NOW)
    ledger.record_costs(costs("2026-11-09", "1"), source="OPENAI_LIVE", acquired_at=NOW.isoformat())
    ledger.record_costs(costs("2026-11-09", "0"), source="LOCAL_EXPORT", acquired_at=(NOW-timedelta(days=1)).isoformat())
    ledger.record_costs(costs("2026-11-09", "1"), source="OPENAI_LIVE", acquired_at=(NOW+timedelta(minutes=1)).isoformat())
    assert len(repo.all_records("annotations", entity_id=COSTS)) == 1
    assert ledger._daily()["2026-11-09"]["cost_usd"] == "1"


def test_import_overlapping_exports_replaces_not_sums_and_persists_as_pair(tmp_path):
    store = OpenAIExports(tmp_path/"costs.json", tmp_path/"usage.json")
    first = costs("2026-11-08", "1")
    usage = {"data": []}
    store.save(first, usage)
    store.save(first, usage)
    assert len(list((tmp_path/"imports").glob('*.json'))) == 1
    store.save({"data": [*costs("2026-11-08", "2")["data"], *costs("2026-11-09", "3")["data"]]}, usage)
    c, u = store.read()
    assert sum(Decimal(b["results"][0]["amount"]["value"]) for b in c["data"]) == Decimal(5)
    assert u["data"] == usage["data"]
    pointer = json.loads(store.pointer.read_text())
    (tmp_path/"imports"/pointer["name"]).write_text('tampered')
    with pytest.raises(ValueError, match="Integrità"):
        store.read()


def test_partial_or_invalid_exports_do_not_commit(tmp_path):
    store = OpenAIExports(tmp_path/"costs.json", tmp_path/"usage.json")
    old = costs("2026-11-08", "1")
    store.save(old, {"data": []})
    original = store.pointer.read_bytes()
    partial = costs("2026-11-08", "2")
    partial["data"][0]["start_time"] += 3600
    with pytest.raises(ValueError, match="parzialmente"):
        store.save(partial, {"data": []})
    assert store.pointer.read_bytes() == original
    for amount in ("NaN", "Infinity", "bad", "-1"):
        with pytest.raises(ValueError):
            validate_export(costs("2026-11-08", amount), "costs")
    with pytest.raises(ValueError, match="sovrapposti"):
        merge_exports({"data": []}, {"data": old["data"]*2}, "costs")


def test_credit_reference_validates_levels_and_reused_request_payload(repo):
    with pytest.raises(ValueError):
        CreditReference(request_id=uuid4(), amount_usd="7.50", warning_usd="2", critical_usd="5")
    ledger = OpenAICredit(repo)
    request = CreditReference(request_id=uuid4(), amount_usd="7.50")
    ledger.set_reference(request)
    with pytest.raises(ValueError, match="diverso"):
        ledger.set_reference(request.model_copy(update={"amount_usd": Decimal("8")}))


def test_cost_admin_endpoints_require_local_origin_and_csrf_and_keep_bank_empty(repo, tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_COST_USD_EUR_RATE", "0.90")
    app = FastAPI()
    runtime = SimpleNamespace(repository=repo, settings=SimpleNamespace(database_url='sqlite:///:memory:'))
    mount_settings(app, lambda: runtime)
    app.state.api_control = APIControlService(cost_path=tmp_path/'costs.json', usage_path=tmp_path/'usage.json', repository=repo)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 123)) as client:
        request = {"request_id": str(uuid4()), "amount_usd": "7.50"}
        assert client.post('/system/settings/openai-credit', json=request).status_code == 403
        token = client.get('/system/settings').json()['token']
        headers = {'x-better-settings-token': token}
        assert client.post('/system/settings/openai-credit', headers={**headers, 'origin': 'https://evil.test'}, json=request).status_code == 403
        payload = {"costs": costs('2026-11-09', '0.25'), "usage": {"data": []}}
        imported = client.post('/system/settings/openai-exports', headers=headers, json=payload)
        assert imported.status_code == 200
        saved = client.post('/system/settings/openai-credit', headers=headers, json=request)
        assert saved.status_code == 200 and saved.json()['amount'] == '7.50'
        assert not repo.all_records('bet_events')
