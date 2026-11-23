"""User-declared API credit and dated spending estimates; unrelated to Banca."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import select

from better.domain.common import Contract
from better.storage.db import TABLES
from better.storage.repository import canonical_hash

COSTS = "api-control:openai-daily-costs:v1"
CREDIT = "api-control:openai-credit:v1"
BILLING = "https://platform.openai.com/settings/organization/billing/overview"


class CreditReference(Contract):
    request_id: UUID
    amount_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    warning_usd: Decimal = Field(default=Decimal("5"), ge=0, max_digits=12, decimal_places=2)
    critical_usd: Decimal = Field(default=Decimal("2"), ge=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def ordered_levels(self):
        if self.critical_usd > self.warning_usd:
            raise ValueError("La soglia critica non può superare quella di avviso")
        return self


class OpenAICredit:
    def __init__(self, repository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def _daily(self, connection=None):
        latest = {}
        if connection is None:
            rows = self.repository.all_records("annotations", entity_id=COSTS)
        else:
            table = TABLES["annotations"]
            rows = connection.execute(select(table.c.payload).where(table.c.entity_id == COSTS)).scalars()
        for row in rows:
            if row["day"] not in latest or row["acquired_at"] > latest[row["day"]]["acquired_at"]:
                latest[row["day"]] = row
        return latest

    def record_costs(self, payload, *, source, acquired_at):
        """Snapshots replace overlapping days, never add overlapping exports."""
        daily = {}
        for bucket in payload.get("data", []):
            day = datetime.fromtimestamp(int(bucket["start_time"]), UTC).date().isoformat()
            value = sum((Decimal(str(r["amount"]["value"])) for r in bucket["results"]
                         if r.get("amount", {}).get("currency", "usd").upper() == "USD"), Decimal(0))
            if not value.is_finite() or value < 0:
                raise ValueError("Costo OpenAI non valido")
            daily[day] = daily.get(day, Decimal(0)) + value
        latest = self._daily()
        with self.repository.engine.begin() as c:
            for day, value in daily.items():
                previous = latest.get(day)
                if previous and (previous["acquired_at"] > acquired_at or Decimal(previous["cost_usd"]) == value):
                    continue
                row = {"day": day, "cost_usd": str(value), "source": source, "acquired_at": acquired_at}
                self.repository._append(c, "annotations", identifier="openai-daily:" + canonical_hash(row),
                    entity_id=COSTS, match_id=None, payload=row)

    def set_reference(self, request):
        document = request.model_dump(mode="json")
        with self.repository.engine.begin() as c:
            if self.repository.engine.dialect.name == "sqlite":
                c.exec_driver_sql("BEGIN IMMEDIATE")
            table = TABLES["annotations"]
            rows = list(c.execute(select(table.c.payload).where(table.c.entity_id == CREDIT)).scalars())
            existing = next((r for r in rows if r["request"]["request_id"] == document["request_id"]), None)
            if existing:
                if existing["request"] != document:
                    raise ValueError("Richiesta già utilizzata con un saldo diverso")
                return existing
            row = {"request": document, "at": self.clock().isoformat(),
                   "sequence": max((r["sequence"] for r in rows), default=0) + 1,
                   "baseline_daily": {day: r["cost_usd"] for day, r in self._daily(c).items()},
                   "source": "USER_REPORTED"}
            self.repository._append(c, "annotations", identifier="openai-credit:" + document["request_id"],
                entity_id=CREDIT, match_id=None, payload=row)
            return row

    def report(self, calls=()):
        rows = self.repository.all_records("annotations", entity_id=CREDIT)
        if not rows:
            return {"status": "NOT_AVAILABLE", "amount": None, "currency": "USD", "billing_url": BILLING,
                    "reason": "Saldo non rilevato via API. Registra il credito visualizzato nella fatturazione OpenAI."}
        reference = max(rows, key=lambda r: r["sequence"])
        declared = Decimal(reference["request"]["amount_usd"])
        at = datetime.fromisoformat(reference["at"])
        day = at.astimezone(UTC).date().isoformat()
        latest = self._daily()
        official = sum((max(Decimal(0), Decimal(r["cost_usd"]) - Decimal(reference["baseline_daily"].get(d, "0")))
                        for d, r in latest.items() if d >= day), Decimal(0))
        unique = {}
        for call in calls:
            completed = call.get("completed_at")
            if not completed or datetime.fromisoformat(completed.replace("Z", "+00:00")) <= at:
                continue
            if call.get("cached"):
                continue
            key = call.get("session_id") or call.get("response_id") or call.get("request_id")
            if key:
                unique[key] = call
        local = sum((Decimal(c["cost_usd"]) for c in unique.values() if c.get("cost_usd") is not None), Decimal(0))
        unknown = sum(c.get("cost_usd") is None for c in unique.values())
        # Official totals and per-call estimates overlap. Never sum them.
        consumed = max(official, local)
        remaining = declared - consumed
        critical, warning = (Decimal(reference["request"][k]) for k in ("critical_usd", "warning_usd"))
        level = "CRITICAL" if remaining <= critical else "LOW" if remaining <= warning else "UNCERTAIN" if unknown else "OK"
        return {"status": "ESTIMATED", "amount": str(remaining), "currency": "USD", "billing_url": BILLING,
                "reference_amount": str(declared), "reference_at": reference["at"], "source": "USER_REPORTED",
                "official_spend_since_reference": str(official), "local_estimated_spend_since_reference": str(local),
                "unknown_cost_calls": unknown, "level": level, "warning_usd": str(warning), "critical_usd": str(critical),
                "costs_updated_at": max((r["acquired_at"] for r in latest.values()), default=None),
                "reason": "Stima dal saldo dichiarato e dai costi successivi; non è il saldo letto da OpenAI. "
                          "Contabilizzazione tardiva, consumi esterni e scadenze dei crediti possono cambiarlo. "
                          "Dopo una ricarica registra il nuovo saldo totale."}
