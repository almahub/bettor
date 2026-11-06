"""Fail-closed cross-provider identity resolution.

Candidates may be ranked using names and kickoff proximity, but a mapping is
accepted automatically only when every safety threshold and the runner-up
margin are satisfied. Accepted mappings are append-only and auditable.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher


def normalized_name(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().casefold()
    tokens = re.findall(r"[a-z0-9]+", text)
    noise = {"fc", "cf", "afc", "club", "football", "calcio", "women", "w", "femminile"}
    aliases = {"internazionale": "inter", "milano": "milan"}
    useful = [aliases.get(token, token) for token in tokens if token not in noise]
    return " ".join(useful or tokens)


def name_similarity(left: str | None, right: str | None) -> float:
    a, b = normalized_name(left), normalized_name(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(a.split()), set(b.split())
    token = len(a_tokens & b_tokens) / len(a_tokens | b_tokens) if a_tokens | b_tokens else 0.0
    return max(sequence, token)


@dataclass(frozen=True)
class ResolutionCandidate:
    row: dict
    competitors: dict
    event_id: str
    competition_id: str
    home_id: str
    away_id: str
    home_score: float
    away_score: float
    competition_score: float | None
    kickoff_delta_seconds: float
    score: float

    def diagnostic(self):
        return {
            "event_id": self.event_id,
            "competition_id": self.competition_id,
            "home_id": self.home_id,
            "away_id": self.away_id,
            "score": round(self.score, 6),
            "home_score": round(self.home_score, 6),
            "away_score": round(self.away_score, 6),
            "competition_score": (
                round(self.competition_score, 6) if self.competition_score is not None else None
            ),
            "kickoff_delta_seconds": round(self.kickoff_delta_seconds, 3),
        }


class SportradarIdentityResolver:
    def __init__(
        self,
        *,
        minimum_name_score=0.84,
        automatic_score=0.92,
        minimum_margin=0.08,
        maximum_kickoff_delta_minutes=120,
        verified_kickoff_delta_minutes=20,
    ):
        self.minimum_name_score = minimum_name_score
        self.automatic_score = automatic_score
        self.minimum_margin = minimum_margin
        self.maximum_delta = maximum_kickoff_delta_minutes * 60
        self.verified_delta = verified_kickoff_delta_minutes * 60

    def rank(self, match, rows, *, mapped=None, competition_name=None):
        mapped = mapped or {}
        candidates = []
        for row in rows:
            try:
                event = row["sport_event"]
                competitors = {item["qualifier"]: item for item in event["competitors"]}
                context = event["sport_event_context"]
                kickoff = datetime.fromisoformat(event["start_time"])
                delta = abs((kickoff - match.kickoff).total_seconds())
                home, away = competitors["home"], competitors["away"]
                competition = context["competition"]
                if delta > self.maximum_delta:
                    continue
                if mapped.get("match") and event["id"] != mapped["match"]:
                    continue
                if mapped.get("home") and home["id"] != mapped["home"]:
                    continue
                if mapped.get("away") and away["id"] != mapped["away"]:
                    continue
                if mapped.get("competition") and competition["id"] != mapped["competition"]:
                    continue
                hs = 1.0 if mapped.get("home") else name_similarity(match.home_team, home.get("name"))
                aws = 1.0 if mapped.get("away") else name_similarity(match.away_team, away.get("name"))
                cs = (
                    1.0
                    if mapped.get("competition")
                    else name_similarity(competition_name, competition.get("name"))
                    if competition_name
                    else None
                )
                if hs < self.minimum_name_score or aws < self.minimum_name_score:
                    continue
                time_score = max(0.0, 1.0 - delta / self.maximum_delta)
                components = [(hs, 0.35), (aws, 0.35), (time_score, 0.2)]
                if cs is not None:
                    components.append((cs, 0.1))
                else:
                    components.append((1.0, 0.1))
                score = sum(value * weight for value, weight in components)
                candidates.append(
                    ResolutionCandidate(
                        row,
                        competitors,
                        event["id"],
                        competition["id"],
                        home["id"],
                        away["id"],
                        hs,
                        aws,
                        cs,
                        delta,
                        score,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(candidates, key=lambda item: (-item.score, item.event_id))

    def decide(self, candidates, *, has_verified_mapping=False):
        if not candidates:
            return None, "NO_CANDIDATE"
        best = candidates[0]
        if has_verified_mapping:
            return (best, "VERIFIED_MAPPING") if len(candidates) == 1 else (None, "AMBIGUOUS_VERIFIED")
        if best.kickoff_delta_seconds > self.verified_delta:
            return None, "KICKOFF_OUTSIDE_AUTO_WINDOW"
        if best.competition_score is not None and best.competition_score < 0.75:
            return None, "COMPETITION_MISMATCH"
        if best.score < self.automatic_score:
            return None, "LOW_CONFIDENCE"
        if len(candidates) > 1 and best.score - candidates[1].score < self.minimum_margin:
            return None, "AMBIGUOUS_CANDIDATES"
        return best, "AUTO_VERIFIED"
