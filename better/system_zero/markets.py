"""Canonical, explicitly supported markets and complete-set checks."""

from collections.abc import Mapping

from .math import decimal_odds

OUTCOMES = {
    "1X2": frozenset({"HOME", "DRAW", "AWAY"}),
    "OVER_UNDER_2_5": frozenset({"OVER_2_5", "UNDER_2_5"}),
    "BTTS": frozenset({"YES", "NO"}),
}

_ALIASES = {
    "1x2": "1X2",
    "match_winner": "1X2",
    "h2h": "1X2",
    "over_under_2_5": "OVER_UNDER_2_5",
    "over/under 2.5": "OVER_UNDER_2_5",
    "btts": "BTTS",
    "both teams to score": "BTTS",
}


def canonical_market(name: str) -> str:
    try:
        return _ALIASES[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported market: {name}") from exc


def validate_complete_market(market: str, odds: Mapping[str, float]) -> None:
    market = canonical_market(market)
    if set(odds) != OUTCOMES[market]:
        raise ValueError(f"incomplete or inconsistent outcome set for {market}")
    for value in odds.values():
        decimal_odds(value)


def equivalent_selection(market: str, selection: str, line: float | None = None) -> str:
    """Conservative equivalence; all other same-match bets still share exposure caps."""
    if market == "ASIAN_HANDICAP" and line == -0.5 and selection in {"HOME", "AWAY"}:
        return f"1X2:{selection}"
    return f"{market}:{selection}" + (f":{line}" if line is not None else "")
