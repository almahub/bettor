import json
import os

import httpx
import pytest

from better.services.api_control import APIControlService
from better.storage.repository import Repository


def export(bucket_result):
    return {
        "data": [
            {
                "start_time": 1790812800,
                "start_time_iso": "2026-10-01T00:00:00+00:00",
                "results": [bucket_result],
            }
        ]
    }


@pytest.mark.asyncio
async def test_local_exports_are_aggregated_without_identifiers(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    monkeypatch.setenv("OPENAI_COST_USD_EUR_RATE", "0.90")
    costs = tmp_path / "costs.json"
    usage = tmp_path / "usage.json"
    costs.write_text(
        json.dumps(export({"amount": {"value": "1.25", "currency": "usd"}, "api_key_id": "secret"}))
    )
    usage.write_text(
        json.dumps(
            export(
                {
                    "model": "model-a",
                    "api_source": "agents_api",
                    "num_model_requests": 2,
                    "input_tokens": 100,
                    "input_cached_tokens": 40,
                    "input_cache_write_tokens": 20,
                    "output_tokens": 10,
                    "api_key_id": "secret",
                }
            )
        )
    )
    report = await APIControlService(cost_path=costs, usage_path=usage).report()
    assert report["summary"]["requests"] == 2
    assert report["summary"]["total_tokens"] == 110
    assert report["summary"]["costs"] == {"USD": "1.25"}
    assert report["summary"]["cost_eur"] == "1.1250"
    assert report["credit_balance"]["amount"] is None
    assert report["credit_balance"]["status"] == "NOT_AVAILABLE"
    assert report["credit_balance"]["billing_url"].startswith('https://platform.openai.com/')
    assert "secret" not in json.dumps(report)


@pytest.mark.asyncio
async def test_live_refresh_uses_admin_endpoints(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
    monkeypatch.setenv("OPENAI_USAGE_LIVE_ENABLED", "true")
    monkeypatch.setenv("OPENAI_COST_USD_EUR_RATE", "0.90")

    def handler(request):
        assert request.headers["authorization"] == "Bearer sk-admin-test"
        if request.url.path.endswith("/organization/costs"):
            return httpx.Response(200, json=export({"amount": {"value": "0.50", "currency": "usd"}}))
        return httpx.Response(
            200,
            json=export(
                {
                    "model": "model-live",
                    "api_source": "agents_api",
                    "num_model_requests": 3,
                    "input_tokens": 50,
                    "output_tokens": 5,
                }
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        report = await APIControlService(
            cost_path=tmp_path / "absent-costs.json",
            usage_path=tmp_path / "absent-usage.json",
            client=client,
        ).report()
    finally:
        await client.aclose()
    assert report["source"] == "OPENAI_LIVE"
    assert report["summary"]["requests"] == 3
    assert report["summary"]["costs"] == {"USD": "0.50"}
    assert report["summary"]["cost_eur"] == "0.4500"


@pytest.mark.parametrize(("status", "provider_code", "expected"), [
    (401, "invalid_api_key", "OPENAI_ADMIN_AUTH_ERROR"),
    (403, "permission_denied", "OPENAI_ADMIN_PERMISSION_ERROR"),
    (429, "insufficient_quota", "OPENAI_QUOTA_EXHAUSTED"),
    (429, "rate_limit_exceeded", "OPENAI_RATE_LIMIT"),
    (500, "server_error", "OPENAI_USAGE_HTTP_ERROR"),
])
async def test_refresh_failure_has_safe_cause_and_keeps_export_date(tmp_path, monkeypatch, status, provider_code, expected):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-secret-test")
    monkeypatch.setenv("OPENAI_USAGE_LIVE_ENABLED", "true")
    monkeypatch.setenv("OPENAI_COST_USD_EUR_RATE", "0.90")
    paths = [tmp_path / "costs.json", tmp_path / "usage.json"]
    for path in paths:
        path.write_text(json.dumps({"data": []}))
        os.utime(path, (1790812800, 1790812800))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": provider_code, "message": "Rejected admin-secret-test credential"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await APIControlService(cost_path=paths[0], usage_path=paths[1], client=client).report()
    assert len(requests) == 1
    assert report["source"] == "LOCAL_EXPORT"
    assert report["refresh_error"]["code"] == expected
    assert report["refresh_error"]["http_status"] == status
    assert report["refresh_error"]["endpoint"] == "/v1/organization/costs"
    assert report["updated_at"] == "2026-10-01T00:00:00+00:00"
    assert report["checked_at"] != report["updated_at"]
    assert "admin-secret-test" not in json.dumps(report)
    assert "HTTPStatusError" not in report["warning"]


def test_network_failure_is_not_reported_as_authentication():
    detail = APIControlService._refresh_error(httpx.ConnectError("connection blocked"))
    assert detail["code"] == "OPENAI_USAGE_CONNECTION_ERROR"
    assert detail["http_status"] is None


def test_archiving_local_call_history_hides_it_without_deleting_audit():
    repository = Repository("sqlite:///:memory:")
    document = {
        "match_id": "old-match",
        "status": "EXTERNAL_RESEARCH_ERROR",
        "completed_at": "2026-10-01T10:00:00+00:00",
        "request_count": 1,
        "error": "old credential failure",
    }
    with repository.engine.begin() as connection:
        repository._append(
            connection,
            "research_results",
            identifier="old-error",
            entity_id="old-request",
            match_id="old-match",
            payload=document,
        )
    service = APIControlService(repository=repository)
    assert len(service._local_calls(None)) == 1

    service.archive_local_history()

    assert service._local_calls(None) == []
    assert len(repository.list_records("research_results")) == 1
    assert repository.list_records("annotations", entity_id="api-control:openai-history")[0][
        "kind"
    ] == "OPENAI_HISTORY_ARCHIVED"
