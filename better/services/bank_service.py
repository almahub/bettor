"""User-declared EUR ledger in existing append-only storage; no bookmaker execution."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import select

from better.domain.common import Contract
from better.domain.models import Market, Portfolio, Position
from better.storage.db import TABLES

ENTITY = "bank:EUR:v1"


class BankInitialize(Contract):
    request_id: UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class BankTransfer(Contract):
    request_id: UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class BankBet(Contract):
    request_id: UUID
    match_id: str = Field(min_length=1)
    market: Market
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    odds: Decimal = Field(gt=1, le=10000, decimal_places=4)


class BankSlipBet(Contract):
    request_id: UUID
    betslip_id: UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    odds: Decimal = Field(gt=1, le=1000000)
    bookmaker: str = Field(min_length=1, max_length=120)
    kickoff: datetime
    match_id: str = Field(min_length=1)
    team_ids: tuple[str, ...] = Field(min_length=1)
    competition: str = Field(min_length=1)
    market: Market


class BankSettlement(Contract):
    request_id: UUID
    result: Literal["WON", "LOST", "VOID"]


class BankBetCancellation(Contract):
    request_id: UUID


class BankService:
    def __init__(self, repository, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def _events(self, connection, as_of=None):
        table = TABLES["annotations"]
        rows = connection.execute(select(table.c.payload).where(table.c.entity_id == ENTITY)).scalars()
        return sorted(
            (r for r in rows if as_of is None or datetime.fromisoformat(r["at"]) <= as_of),
            key=lambda r: r["sequence"],
        )

    def _state(self, events):
        cash = initial = equity = peak = Decimal(0)
        bets, profit = {}, Decimal(0)
        cancelled = {event["bet_id"] for event in events if event["kind"] == "BET_CANCELLED"}
        for event in events:
            if event["kind"] == "INITIAL":
                cash = initial = equity = peak = Decimal(event["amount"])
            elif event["kind"] == "DEPOSIT":
                amount = Decimal(event["amount"])
                cash += amount
                equity += amount
            elif event["kind"] == "WITHDRAWAL":
                amount = Decimal(event["amount"])
                cash -= amount
                equity -= amount
            elif event["kind"] == "BET":
                if event["request_id"] in cancelled:
                    continue
                cash -= Decimal(event["amount"])
                bets[event["request_id"]] = {**event, "status": "OPEN", "settled_at": None, "return": None}
            elif event["kind"] == "SETTLEMENT":
                if event["bet_id"] in cancelled:
                    continue
                bet = bets[event["bet_id"]]
                paid = Decimal(event["return"])
                gain = paid - Decimal(bet["amount"])
                cash += paid
                equity += gain
                profit += gain
                peak = max(peak, initial + profit)
                bet.update(status=event["result"], settled_at=event["at"], **{"return": str(paid)})
        reserved = sum((Decimal(b["amount"]) for b in bets.values() if b["status"] == "OPEN"), Decimal(0))
        return {
            "initialized": bool(events),
            "currency": "EUR",
            "initial_budget": str(initial),
            "available": str(cash),
            "reserved": str(reserved),
            "equity": str(equity),
            "profit": str(profit),
            "peak": str(peak),
            "drawdown": min(1.0, max(0.0, float((peak - initial - profit) / peak))) if peak else 0,
            "bets": [bet for bet in bets.values() if bet["status"] != "CANCELLED"],
            "transfers": [
                {"kind": event["kind"], "amount": event["amount"], "at": event["at"]}
                for event in reversed(events)
                if event["kind"] in {"DEPOSIT", "WITHDRAWAL"}
            ][:50],
            "event_count": len(events),
            "origin": "user_declared",
            "automatic_execution": False,
        }

    def status(self, as_of=None):
        with self.repository.engine.connect() as connection:
            return self._state(self._events(connection, as_of))

    def _write(self, request, kind, extra=None):
        payload = request.model_dump(mode="json")
        with self.repository.engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(72819432)")
            try:
                events = self._events(connection)
                # Retry the same request safely, rejecting reuse with different contents.
                old = next((e for e in events if e["request_id"] == str(request.request_id)), None)
                if old:
                    if old["input"] != {"kind": kind, **payload, **(extra or {})}:
                        raise ValueError("Identificativo richiesta gia utilizzato con dati diversi")
                    connection.commit()
                    return self._state(events)
                state = self._state(events)
                event = {
                    "kind": kind,
                    **payload,
                    **(extra or {}),
                    "sequence": len(events) + 1,
                    "at": self.clock().isoformat(),
                    "input": {"kind": kind, **payload, **(extra or {})},
                }
                if kind == "INITIAL":
                    if state["initialized"]:
                        raise ValueError("Banca gia inizializzata; il budget iniziale non si sovrascrive")
                elif not state["initialized"]:
                    raise ValueError("Impostare prima il budget iniziale")
                elif kind == "BET":
                    if request.amount > Decimal(state["available"]):
                        raise ValueError("Saldo disponibile insufficiente")
                elif kind == "WITHDRAWAL":
                    if request.amount > Decimal(state["available"]):
                        raise ValueError("Saldo disponibile insufficiente per il prelievo")
                elif kind == "SETTLEMENT":
                    bet = next((b for b in state["bets"] if b["request_id"] == extra["bet_id"]), None)
                    if bet is None or bet["status"] != "OPEN":
                        raise ValueError("Puntata inesistente o gia liquidata")
                    if request.result != "VOID" and self.clock() < datetime.fromisoformat(bet["kickoff"]):
                        raise ValueError("Non si puo registrare un esito prima del calcio di inizio")
                    stake = Decimal(bet["amount"])
                    paid = (
                        Decimal(extra["return_amount"])
                        if extra and extra.get("return_amount") is not None
                        else stake * Decimal(bet["odds"])
                        if request.result == "WON"
                        else (stake if request.result == "VOID" else Decimal(0))
                    )
                    if paid < 0:
                        raise ValueError("Il ritorno non può essere negativo")
                    event["return"] = str(paid.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                elif kind == "BET_CANCELLED":
                    bet = next(
                        (
                            item
                            for item in events
                            if item["kind"] == "BET" and item["request_id"] == extra["bet_id"]
                        ),
                        None,
                    )
                    if bet is None:
                        raise ValueError("Puntata manuale inesistente")
                    if bet.get("source") == "SALABET":
                        raise ValueError("Le puntate originate da SalaBet non si eliminano dalla Banca")
                    if any(
                        item["kind"] == "BET_CANCELLED" and item["bet_id"] == extra["bet_id"]
                        for item in events
                    ):
                        raise ValueError("Puntata manuale gia eliminata")
                self.repository._append(
                    connection,
                    "annotations",
                    identifier=f"bank:{request.request_id}",
                    entity_id=ENTITY,
                    match_id=event.get("match_id"),
                    payload=event,
                )
                connection.commit()
                return self._state([*events, event])
            except Exception:
                connection.rollback()
                raise

    def initialize(self, request):
        return self._write(request, "INITIAL")

    def deposit(self, request: BankTransfer):
        return self._write(request, "DEPOSIT")

    def withdraw(self, request: BankTransfer):
        return self._write(request, "WITHDRAWAL")

    def record_bet(self, request, match):
        if request.match_id != match.match_id or self.clock() >= match.kickoff:
            raise ValueError("Registrazione consentita solo per la partita selezionata prima del kickoff")
        if request.market.type.value not in {"1X2", "OVER_UNDER_2_5", "BTTS"}:
            raise ValueError("Mercato non supportato dalla Banca")
        return self._write(
            request,
            "BET",
            {
                "source": "MANUAL",
                "team_ids": [match.home_team_id, match.away_team_id],
                "competition": match.competition,
                "home_team": match.home_team,
                "away_team": match.away_team,
                "kickoff": match.kickoff.isoformat(),
            },
        )

    def record_slip(self, request: BankSlipBet):
        if request.kickoff.tzinfo is None or self.clock() >= request.kickoff:
            raise ValueError("Registrazione SalaBet consentita solo prima del primo kickoff")
        return self._write(
            request,
            "BET",
            {
                "source": "SALABET",
                "team_ids": list(request.team_ids),
                "home_team": "SalaBet",
                "away_team": request.bookmaker,
                "kickoff": request.kickoff.isoformat(),
            },
        )

    def settle(self, bet_id, request):
        return self._write(request, "SETTLEMENT", {"bet_id": str(bet_id)})

    def settle_slip(self, bet_id, request, return_amount: Decimal):
        return self._write(
            request,
            "SETTLEMENT",
            {"bet_id": str(bet_id), "return_amount": str(return_amount)},
        )

    def cancel_manual_bet(self, bet_id, request: BankBetCancellation):
        return self._write(request, "BET_CANCELLED", {"bet_id": str(bet_id)})

    def timeline(self):
        with self.repository.engine.connect() as connection:
            events = self._events(connection)
        return [
            {
                "at": event["at"],
                "kind": event["kind"],
                "available": self._state(events[: index + 1])["available"],
                "equity": self._state(events[: index + 1])["equity"],
            }
            for index, event in enumerate(events)
        ]

    def portfolio(self, as_of):
        with self.repository.engine.connect() as connection:
            events = self._events(connection, as_of)
        state = self._state(events)
        if not state["initialized"]:
            return None
        equity = Decimal(state["equity"])
        positions = tuple(
            Position(
                match_id=b["match_id"],
                team_ids=tuple(b["team_ids"]),
                competition=b["competition"],
                market=Market.model_validate(b["market"]),
                bankroll_fraction=min(float(Decimal(b["amount"]) / equity), 1) if equity else 1,
                stake_amount=Decimal(b["amount"]),
                placed_at=datetime.fromisoformat(b["at"]),
                settled_at=datetime.fromisoformat(b["settled_at"]) if b["settled_at"] else None,
            )
            for b in state["bets"]
        )

        def loss(days):
            cutoff = as_of - timedelta(days=days)
            prior = self._state([e for e in events if datetime.fromisoformat(e["at"]) < cutoff])
            start = Decimal(prior["equity"]) if prior["initialized"] else Decimal(state["initial_budget"])
            return min(1, max(0, float((start - equity) / start))) if start else 0

        return Portfolio(
            bankroll=equity,
            positions=positions,
            drawdown=state["drawdown"],
            daily_loss_fraction=loss(1),
            weekly_loss_fraction=loss(7),
        )
