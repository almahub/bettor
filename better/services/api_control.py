"""OpenAI organization usage/cost reporting with an offline export fallback."""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from better.services.local_environment import environment_value
from better.storage.repository import canonical_hash


class APIControlService:
    def __init__(
        self,
        *,
        cost_path: str | Path = "data/api-control/costs.json",
        usage_path: str | Path = "data/api-control/completions-usage.json",
        fx_path: str | Path = "data/api-control/eur-rate.json",
        client: httpx.AsyncClient | None = None,
        repository=None,
    ):
        self.cost_path = Path(cost_path)
        self.usage_path = Path(usage_path)
        self.fx_path = Path(fx_path)
        self.client = client
        self.repository = repository

    async def report(self):
        admin_key = environment_value("OPENAI_ADMIN_KEY")
        live_enabled = environment_value("OPENAI_USAGE_LIVE_ENABLED").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        warning = None
        refresh_error = None
        if admin_key and live_enabled:
            try:
                costs, usage = await self._fetch_live(admin_key)
                source = "OPENAI_LIVE"
            except Exception as error:
                costs, usage = self._read_exports()
                source = "LOCAL_EXPORT"
                refresh_error = self._refresh_error(error)
                warning = refresh_error["message"] + " Sono mostrati gli export locali, non i consumi aggiornati."
        else:
            costs, usage = self._read_exports()
            source = "LOCAL_EXPORT"
            if not admin_key:
                warning = "OPENAI_ADMIN_KEY not configured; local exports shown."
            elif not live_enabled:
                warning = "Live usage refresh disabled; set OPENAI_USAGE_LIVE_ENABLED=true."
        eur_rate, eur_rate_source = await self._eur_rate()
        report = self._normalize(
            costs, usage, source, warning, bool(admin_key), live_enabled, eur_rate, eur_rate_source
        )
        report["refresh_error"] = refresh_error
        report["checked_at"] = report["updated_at"]
        if source == "LOCAL_EXPORT":
            timestamps = []
            for path in (self.cost_path, self.usage_path):
                try:
                    timestamps.append(path.stat().st_mtime)
                except OSError:
                    pass
            report["updated_at"] = datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat() if timestamps else None
        report["calls"] = self._local_calls(eur_rate)
        report["credit_balance"] = {
            "status": "NOT_AVAILABLE", "amount": None, "currency": "USD",
            "reason": "Saldo residuo non disponibile tramite le API ufficiali integrate. Consultare la fatturazione OpenAI.",
            "billing_url": "https://platform.openai.com/settings/organization/billing/overview",
        }
        return report

    @staticmethod
    def _refresh_error(error):
        # Never expose the raw response: auth errors can echo credential fragments.
        status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        endpoint = error.request.url.path if isinstance(error, httpx.HTTPStatusError) else None
        if status == 401:
            code, message = "OPENAI_ADMIN_AUTH_ERROR", "Monitoraggio OpenAI: autenticazione amministrativa fallita (HTTP 401). Verificare OPENAI_ADMIN_KEY."
        elif status == 403:
            code, message = "OPENAI_ADMIN_PERMISSION_ERROR", "Monitoraggio OpenAI: accesso amministrativo non autorizzato (HTTP 403). Verificare i permessi della chiave."
        elif status == 429:
            try:
                body = error.response.json()
                quota = isinstance(body, dict) and isinstance(body.get("error"), dict) and body["error"].get("code") == "insufficient_quota"
            except ValueError:
                quota = False
            code = "OPENAI_QUOTA_EXHAUSTED" if quota else "OPENAI_RATE_LIMIT"
            message = "Monitoraggio OpenAI: quota API esaurita (HTTP 429)." if quota else "Monitoraggio OpenAI: limite di richieste raggiunto (HTTP 429). Riprovare piu tardi."
        elif status:
            code, message = "OPENAI_USAGE_HTTP_ERROR", f"Monitoraggio OpenAI: richiesta rifiutata (HTTP {status})."
        elif isinstance(error, httpx.TransportError):
            code, message = "OPENAI_USAGE_CONNECTION_ERROR", "Monitoraggio OpenAI: connessione non riuscita."
        else:
            code, message = "OPENAI_USAGE_REFRESH_ERROR", "Monitoraggio OpenAI: aggiornamento dei consumi non riuscito."
        return {"code": code, "message": message, "http_status": status, "endpoint": endpoint, "scope": "ORGANIZATION_USAGE"}

    async def _eur_rate(self):
        configured = environment_value("OPENAI_COST_USD_EUR_RATE")
        if configured:
            return Decimal(configured), "CONFIGURED"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml")
                response.raise_for_status()
            root = ET.fromstring(response.content)
            usd_per_eur = next(
                Decimal(node.attrib["rate"]) for node in root.iter() if node.attrib.get("currency") == "USD"
            )
            rate = Decimal(1) / usd_per_eur
            self.fx_path.parent.mkdir(parents=True, exist_ok=True)
            self.fx_path.write_text(
                json.dumps(
                    {"usd_eur_rate": str(rate), "retrieved_at": datetime.now(timezone.utc).isoformat()}
                ),
                encoding="utf-8",
            )
            return rate, "ECB_DAILY_REFERENCE"
        except Exception:
            try:
                cached = json.loads(self.fx_path.read_text(encoding="utf-8"))
                return Decimal(cached["usd_eur_rate"]), "ECB_CACHED_REFERENCE"
            except Exception:
                return None, "UNAVAILABLE"

    def _read_exports(self):
        return self._read_json(self.cost_path), self._read_json(self.usage_path)

    @staticmethod
    def _read_json(path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"data": []}
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError(f"Invalid OpenAI usage export: {path}")
        return payload

    async def _fetch_live(self, admin_key):
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)
        headers = {"Authorization": f"Bearer {admin_key}"}
        base_url = environment_value("OPENAI_ADMIN_BASE_URL") or "https://api.openai.com/v1"
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=20)
        try:
            common = {"start_time": int(start.timestamp()), "end_time": int(now.timestamp())}
            cost_response = await client.get(
                f"{base_url.rstrip('/')}/organization/costs",
                params={**common, "bucket_width": "1d", "limit": 31},
                headers=headers,
            )
            cost_response.raise_for_status()
            usage_response = await client.get(
                f"{base_url.rstrip('/')}/organization/usage/completions",
                params=[
                    *common.items(),
                    ("bucket_width", "1d"),
                    ("limit", 31),
                    ("group_by", "model"),
                    ("group_by", "api_source"),
                ],
                headers=headers,
            )
            usage_response.raise_for_status()
            return cost_response.json(), usage_response.json()
        finally:
            if own_client:
                await client.aclose()

    @staticmethod
    def _normalize(costs, usage, source, warning, admin_configured, live_enabled, eur_rate, eur_rate_source):
        activity = []
        for bucket in usage.get("data", []):
            day = bucket.get("start_time_iso") or APIControlService._iso(bucket.get("start_time"))
            for row in bucket.get("results", []):
                input_tokens = int(row.get("input_tokens") or 0)
                output_tokens = int(row.get("output_tokens") or 0)
                activity.append(
                    {
                        "date": day,
                        "model": row.get("model") or "unknown",
                        "api_source": row.get("api_source") or "unknown",
                        "requests": int(row.get("num_model_requests") or 0),
                        "input_tokens": input_tokens,
                        "cached_input_tokens": int(row.get("input_cached_tokens") or 0),
                        "cache_write_tokens": int(row.get("input_cache_write_tokens") or 0),
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "service_tier": row.get("service_tier") or "unknown",
                    }
                )
        cost_rows = []
        for bucket in costs.get("data", []):
            day = bucket.get("start_time_iso") or APIControlService._iso(bucket.get("start_time"))
            for row in bucket.get("results", []):
                amount = row.get("amount") or {}
                cost_rows.append(
                    {
                        "date": day,
                        "amount": str(amount.get("value") or "0"),
                        "currency": (amount.get("currency") or "usd").upper(),
                        "line_item": row.get("line_item") or "OpenAI API usage",
                        "api_source": row.get("api_source") or "all",
                        "project": row.get("project_name") or "all",
                        "amount_eur": str(Decimal(str(amount.get("value") or "0")) * eur_rate)
                        if eur_rate is not None
                        else None,
                    }
                )
        currencies = sorted({row["currency"] for row in cost_rows})
        totals_by_currency = {
            currency: str(sum(Decimal(row["amount"]) for row in cost_rows if row["currency"] == currency))
            for currency in currencies
        }
        total_eur = (
            str(sum((Decimal(row["amount_eur"]) for row in cost_rows), Decimal(0)))
            if eur_rate is not None
            else None
        )
        dates = [row["date"] for row in activity + cost_rows if row["date"]]
        return {
            "provider": "OpenAI",
            "source": source,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "period_start": min(dates) if dates else None,
            "period_end": max(dates) if dates else None,
            "admin_key_configured": admin_configured,
            "live_enabled": live_enabled,
            "warning": warning,
            "usd_eur_rate": str(eur_rate) if eur_rate is not None else None,
            "eur_rate_source": eur_rate_source,
            "summary": {
                "requests": sum(row["requests"] for row in activity),
                "input_tokens": sum(row["input_tokens"] for row in activity),
                "cached_input_tokens": sum(row["cached_input_tokens"] for row in activity),
                "cache_write_tokens": sum(row["cache_write_tokens"] for row in activity),
                "output_tokens": sum(row["output_tokens"] for row in activity),
                "total_tokens": sum(row["total_tokens"] for row in activity),
                "costs": totals_by_currency,
                "cost_eur": total_eur,
            },
            "activity": sorted(activity, key=lambda row: (row["date"] or "", row["model"]), reverse=True),
            "costs": sorted(cost_rows, key=lambda row: row["date"] or "", reverse=True),
        }

    def _local_calls(self, eur_rate):
        if self.repository is None:
            return []
        identities = {}
        for run in self.repository.list_records("analysis_runs", last=1000):
            match = (run.get("snapshot") or {}).get("match") or {}
            if match.get("match_id"):
                identities[match["match_id"]] = match
        for request in self.repository.list_records("research_requests", last=1000):
            identities.setdefault(request.get("match_id"), request)
        rows = [
            ("CONTEXT_RESEARCH", row) for row in self.repository.list_records("research_results", last=1000)
        ]
        rows += [
            ("FOOTBALL_DATA_RESEARCH", row)
            for row in self.repository.list_records("annotations", last=1000)
            if row.get("agent_id") and row.get("request_id") and "requested_fields" in row
        ]
        resets = [
            row
            for row in self.repository.list_records(
                "annotations", last=1000, entity_id="api-control:openai-history"
            )
            if row.get("kind") == "OPENAI_HISTORY_ARCHIVED"
        ]
        cutoff = max((row["reset_at"] for row in resets), default=None)
        if cutoff:
            rows = [(kind, row) for kind, row in rows if (row.get("completed_at") or "") > cutoff]
        calls = []
        for kind, row in rows:
            if not any(
                row.get(key)
                for key in (
                    "session_id",
                    "response_id",
                    "request_count",
                    "search_calls",
                    "tool_calls",
                    "input_tokens",
                    "output_tokens",
                )
            ):
                continue
            identity = identities.get(row.get("match_id")) or {}
            cost_usd = row.get("estimated_cost")
            calls.append(
                {
                    "completed_at": row.get("completed_at"),
                    "match_id": row.get("match_id"),
                    "match": " - ".join(
                        value for value in (identity.get("home_team"), identity.get("away_team")) if value
                    )
                    or row.get("match_id"),
                    "competition": identity.get("competition"),
                    "research_type": kind,
                    "status": row.get("status"),
                    "fields": row.get("searched_fields") or row.get("requested_fields") or [],
                    "agent_id": row.get("agent_id"),
                    "session_id": row.get("session_id"),
                    "response_id": row.get("response_id"),
                    "search_calls": int(row.get("search_calls") or 0),
                    "tool_calls": int(row.get("tool_calls") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "duration_ms": row.get("duration_ms"),
                    "cost_usd": str(cost_usd) if cost_usd is not None else None,
                    "cost_eur": str(Decimal(str(cost_usd)) * eur_rate)
                    if cost_usd is not None and eur_rate is not None
                    else None,
                    "cost_status": "ESTIMATED" if cost_usd is not None else "NOT_ATTRIBUTABLE",
                }
            )
        return sorted(calls, key=lambda row: row["completed_at"] or "", reverse=True)

    def archive_local_history(self):
        if self.repository is None:
            raise ValueError("Repository unavailable")
        document = {
            "kind": "OPENAI_HISTORY_ARCHIVED",
            "reset_at": datetime.now(timezone.utc).isoformat(),
            "note": "Previous local call history hidden from API Control; immutable audit retained.",
        }
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "annotations",
                identifier="api-control-history:" + canonical_hash(document),
                entity_id="api-control:openai-history",
                match_id=None,
                payload=document,
            )
        return document

    @staticmethod
    def _iso(timestamp):
        if timestamp is None:
            return None
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()
