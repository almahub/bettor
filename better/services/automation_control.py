"""Persistent automation controls and conservative research reservations, separate from Banca."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field, StrictBool
from sqlalchemy import select

from better.domain.common import Contract
from better.services.local_environment import environment_value
from better.storage.db import TABLES
from better.storage.repository import canonical_hash

CONTROL = "automation:controls:v1"
SPEND = "automation:research-spend:v1"
ROME = ZoneInfo("Europe/Rome")


class AutomationSwitch(Contract):
    request_id: UUID
    enabled: StrictBool


class ResearchLimits(Contract):
    request_id: UUID
    per_match_eur: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    per_day_eur: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    per_week_eur: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class AutomationControl:
    def __init__(self, repository, settings, *, clock=None):
        self.repository, self.settings = repository, settings
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def transaction(self):
        with self.repository.engine.begin() as conn:
            if self.repository.engine.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            yield conn

    def _rows(self, entity, conn=None):
        if conn is None:
            return self.repository.all_records("annotations", entity_id=entity)
        table = TABLES["annotations"]
        return list(conn.execute(select(table.c.payload).where(table.c.entity_id == entity)).scalars())

    def policy(self, conn=None):
        rows = self._rows(CONTROL, conn)
        return max(rows, key=lambda r: r["sequence"]) if rows else {
            "enabled": self.settings.timing_comparison_enabled,
            "per_match_eur": "0.25", "per_day_eur": "5.00", "per_week_eur": "25.00",
            "sequence": 0, "persisted": False,
        }

    def _append(self, conn, entity, identifier, payload, match_id=None):
        self.repository._append(conn, "annotations", identifier=identifier,
            entity_id=entity, match_id=match_id, payload=payload)

    def update(self, request):
        document = request.model_dump(mode="json")
        with self.transaction() as conn:
            prior = self._rows(CONTROL, conn)
            existing = next((r for r in prior if r.get("request_id") == str(request.request_id)), None)
            if existing:
                if existing["request"] != document:
                    raise ValueError("Richiesta già utilizzata con impostazioni diverse")
            else:
                current = self.policy(conn)
                updated = {**current, **{k: v for k, v in document.items() if k != "request_id"},
                    "sequence": current["sequence"] + 1, "persisted": True,
                    "request_id": str(request.request_id), "request": document,
                    "updated_at": self.clock().isoformat()}
                self._append(conn, CONTROL, "automation-control:" + str(request.request_id), updated)
        return self.report()

    def fx(self):
        try:
            configured = environment_value("OPENAI_COST_USD_EUR_RATE")
            if configured:
                rate = Decimal(configured)
                if rate.is_finite() and rate > 0:
                    return rate, "CONFIGURED"
                return None, "UNAVAILABLE"
            cached = json.loads(Path("data/api-control/eur-rate.json").read_text(encoding="utf-8"))
            acquired = datetime.fromisoformat(cached["retrieved_at"].replace("Z", "+00:00"))
            rate = Decimal(cached["usd_eur_rate"])
            if not rate.is_finite() or rate <= 0 or not timedelta(0) <= self.clock() - acquired <= timedelta(days=7):
                return None, "STALE"
            return rate, "ECB_CACHED_REFERENCE"
        except (ValueError, KeyError, OSError, TypeError, InvalidOperation):
            return None, "UNAVAILABLE"

    def _effective(self, conn=None):
        rows = self._rows(SPEND, conn)
        completed = {r["reservation_id"]: r for r in rows if r["kind"] == "COMPLETED"}
        return [{**r, **completed.get(r["reservation_id"], {})} for r in rows if r["kind"] == "RESERVED"]

    def _period(self, row, period):
        then = datetime.fromisoformat(row["at"]).astimezone(ROME)
        now = self.clock().astimezone(ROME)
        return then.date() == now.date() if period == "day" else then.isocalendar()[:2] == now.isocalendar()[:2]

    def report(self):
        policy = self.policy()
        rate, source = self.fx()
        spend = self._effective()
        return {**{k: policy[k] for k in ("enabled", "per_match_eur", "per_day_eur", "per_week_eur")},
            "currency": "EUR", "updated_at": policy.get("updated_at"),
            "paid_research": bool(policy["enabled"] and policy.get("persisted") and rate
                and all(Decimal(policy[k]) > 0 for k in ("per_match_eur", "per_day_eur", "per_week_eur"))),
            "usd_eur_rate": str(rate) if rate else None, "fx_source": source,
            "day_committed_eur": str(sum((Decimal(r["committed_eur"]) for r in spend if self._period(r, "day")), Decimal(0))),
            "week_committed_eur": str(sum((Decimal(r["committed_eur"]) for r in spend if self._period(r, "week")), Decimal(0))),
            "uncertain_reservations": sum(r.get("actual_usd") is None for r in spend),
            "usd_safety_caps": {"per_match": self.settings.validation_research_budget_per_match,
                "per_day": self.settings.validation_research_budget_per_day,
                "per_week": self.settings.validation_research_budget_per_week},
            "scope": "Ricerche automatiche Serie A T24; non puntate, non analisi manuali",
            "last_block": max((r for r in self._rows(SPEND) if r["kind"] == "BLOCKED"), key=lambda r: r["at"], default=None)}

    def guard(self, match, checkpoint):
        return ResearchBudgetGuard(self, match, checkpoint)

    async def ensure_fx(self):
        if self.fx()[0] is None:
            from better.services.api_control import APIControlService
            await APIControlService()._eur_rate()


class ResearchBudgetGuard:
    def __init__(self, control, match, checkpoint):
        self.control, self.match, self.checkpoint = control, match, checkpoint
        self.calls = []

    def summary(self):
        return {"calls": sum(not r.get("cached") for r in self.calls), "cost_known": all(r["cost"] is not None for r in self.calls),
            "known_cost_usd": float(sum((r["cost"] for r in self.calls if r["cost"] is not None), Decimal(0)))}

    def reserve(self, category, maximum_usd):
        c = self.control
        identifier = canonical_hash({"match": self.match.match_id, "kickoff": self.match.kickoff.isoformat(),
                                     "checkpoint": self.checkpoint, "category": category})
        rate, _ = c.fx()
        maximum = Decimal(str(maximum_usd))
        if not maximum.is_finite() or maximum <= 0:
            return None
        with c.transaction() as conn:
            policy = c.policy(conn)
            rows = c._rows(SPEND, conn)
            if any(r.get("reservation_id") == identifier for r in rows):
                return None
            reason = None
            if not policy["enabled"] or not policy.get("persisted"):
                reason = "AUTOMATION_DISABLED"
            elif rate is None:
                reason = "EXCHANGE_RATE_UNAVAILABLE"
            else:
                committed = maximum * rate
                spend = c._effective(conn)
                match_key = self.match.match_id + ":" + self.match.kickoff.isoformat()
                subsets = {"match": [r for r in spend if r["match_key"] == match_key],
                    "day": [r for r in spend if c._period(r, "day")],
                    "week": [r for r in spend if c._period(r, "week")]}
                cost_table = TABLES["validation_cost_events"]
                legacy = list(conn.execute(select(cost_table.c.payload)).scalars())
                for period, records in subsets.items():
                    euro_sum = sum((Decimal(r["committed_eur"]) for r in records), Decimal(0))
                    def in_usd_period(row, period=period):
                        if period == "match":
                            return row["match_id"] == self.match.match_id
                        at = datetime.fromisoformat(row["at"]).astimezone(UTC)
                        now = c.clock().astimezone(UTC)
                        return at.date() == now.date() if period == "day" else at.isocalendar()[:2] == now.isocalendar()[:2]
                    usd_sum = sum((Decimal(r["committed_usd"]) for r in spend if in_usd_period(r)), Decimal(0))
                    usd_sum += sum((Decimal(str(r.get("estimated_cost") or 0)) for r in legacy if in_usd_period(r)), Decimal(0))
                    cap = Decimal(str(getattr(c.settings, "validation_research_budget_per_" + period)))
                    if euro_sum + committed > Decimal(policy["per_" + period + "_eur"]) or usd_sum + maximum > cap:
                        reason = "RESEARCH_BUDGET_EXHAUSTED"
                        break
            if reason:
                c._append(conn, SPEND, "automation-budget-block:" + identifier, {
                    "kind": "BLOCKED", "reservation_id": identifier, "reason": reason,
                    "match_id": self.match.match_id, "checkpoint": self.checkpoint,
                    "at": c.clock().isoformat()})
                return None
            ticket = {"kind": "RESERVED", "reservation_id": identifier,
                "match_id": self.match.match_id, "match_key": match_key,
                "checkpoint": self.checkpoint, "category": category, "at": c.clock().isoformat(),
                "committed_usd": str(maximum), "committed_eur": str(committed), "usd_eur_rate": str(rate),
                "actual_usd": None}
            c._append(conn, SPEND, "automation-budget-reserve:" + identifier, ticket, self.match.match_id)
            return ticket

    def complete(self, ticket, result):
        value = 0 if getattr(result, "cached", False) else getattr(result, "estimated_cost", None)
        actual = Decimal(str(value)) if value is not None else None
        if actual is None or not actual.is_finite() or actual < 0:
            return  # Unknown usage keeps the entire reservation, including after errors/restart.
        row = {"kind": "COMPLETED", "reservation_id": ticket["reservation_id"],
            "committed_usd": str(actual), "committed_eur": str(actual * Decimal(ticket["usd_eur_rate"])),
            "actual_usd": str(actual), "settled_at": self.control.clock().isoformat()}
        with self.control.transaction() as conn:
            self.control._append(conn, SPEND, "automation-budget-complete:" + ticket["reservation_id"], row, self.match.match_id)

    async def execute(self, category, maximum_usd, operation):
        cached = await operation(cache_only=True)
        if cached is not None:
            return cached
        ticket = self.reserve(category, maximum_usd)
        if ticket is None:
            return None
        usage = {"cost": None}
        self.calls.append(usage)
        result = None
        try:
            result = await operation(cache_only=False)
            return result
        finally:
            usage["cached"] = getattr(result, "cached", False)
            value = 0 if usage["cached"] else getattr(result, "estimated_cost", None)
            usage["cost"] = Decimal(str(value)) if value is not None else None
            self.complete(ticket, result)
