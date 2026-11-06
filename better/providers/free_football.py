"""Local-first A/B observations and independently confirmed Serie A results.

OpenFootball CC0 is an acquisition source, never sufficient by itself to settle.
CSV imports require explicit timezone, source and competition; no invented times.
"""

import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from better.domain.common import Contract
from better.domain.enums import ErrorCode, ReliabilityTier, SourceType
from better.domain.errors import BetterError
from better.domain.models import RecentTeamMatch, StatsSnapshot, TeamStats
from better.domain.provenance import Provenance
from better.providers.result_identity import (
    ResultIdentityCandidate,
    ResultProviderIdentity,
    normalize_team_name,
)
from better.providers.results import MatchResult
from better.storage.repository import canonical_hash


class FreeFootballSettings(Contract):
    enabled: bool = False
    competitions: dict[str, str] = Field(default_factory=dict)
    seasons: tuple[int, ...] = (2026, 2025, 2024)
    openfootball_divisions: tuple[str, ...] = ("I1",)
    csv_files: tuple[str, ...] = ()
    refresh_seconds: int = Field(default=21600, ge=300)
    # Reviewed aliases are data, not fuzzy team matching or per-match overrides.
    team_aliases: dict[str, str] = Field(default_factory=dict)


def team_key(name, aliases):
    name = normalize_team_name(name)
    return aliases.get(name, name)


def season_year(kickoff):
    return kickoff.year if kickoff.month >= 7 else kickoff.year - 1


def parse_openfootball(payload, division, season):
    expected = "Serie A" if division == "I1" else "Serie B"
    if expected not in payload.get("name", "") or str(season) not in payload.get("name", ""):
        raise ValueError("OpenFootball competition/season mismatch")
    rows = []
    for row in payload["matches"]:
        if not row.get("time"):
            continue
        raw_score = row.get("score")
        score = raw_score.get("ft") if isinstance(raw_score, dict) else raw_score
        score = [None, None] if score is None else score
        if len(score) != 2 or (score != [None, None] and any(type(n) is not int or not 0 <= n <= 50 for n in score)):
            raise ValueError("Invalid regulation score")
        kickoff = datetime.fromisoformat(row["date"] + "T" + row["time"]).replace(tzinfo=ZoneInfo("Europe/Rome"))
        rows.append({"division": division, "season": season, "kickoff": kickoff.astimezone(UTC).isoformat(),
                     "home": row["team1"], "away": row["team2"], "home_score": score[0], "away_score": score[1],
                     "statistics": {}})
    return rows


def parse_csv(text):
    """Canonical local CSV; timestamps must include an explicit UTC offset.

    Required: division,season,kickoff,home,away,home_score,away_score,source_url.
    Optional observed statistics are kept, never filled with zero.
    """
    rows = []
    for raw in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        division, season = raw["division"], int(raw["season"])
        kickoff = datetime.fromisoformat(raw["kickoff"].replace("Z", "+00:00"))
        if division not in {"I1", "I2"} or kickoff.tzinfo is None or season_year(kickoff) != season:
            raise ValueError("CSV requires A/B, correct season and timezone-aware kickoff")
        if not raw["source_url"].startswith("https://"):
            raise ValueError("CSV requires original HTTPS source")
        scores = [int(raw[k]) for k in ("home_score", "away_score")]
        if any(not 0 <= n <= 50 for n in scores) or raw["home"] == raw["away"]:
            raise ValueError("Invalid CSV teams/score")
        stats = {k: int(raw[k]) for k in ("home_shots", "away_shots", "home_shots_on_target", "away_shots_on_target") if raw.get(k)}
        if any(v < 0 for v in stats.values()):
            raise ValueError("Negative observed statistic")
        rows.append({"division": division, "season": season, "kickoff": kickoff.astimezone(UTC).isoformat(),
                     "home": raw["home"], "away": raw["away"], "home_score": scores[0], "away_score": scores[1],
                     "statistics": stats, "source_url": raw["source_url"]})
    return rows


class FreeFootballProvider:
    provider_id = "free_football"

    def __init__(self, settings, *, repository, fixtures=None, archived=None, client=None, clock=None):
        self.settings, self.repository, self.fixtures, self.archived = settings, repository, fixtures, archived
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._official_index = None
        self._articles = {}
        self.last_refresh = None
        self.football_data_org = None

    def key(self, name):
        return team_key(name, self.settings.team_aliases)

    def competition_target(self, source_id, provider=None):
        division = self.settings.competitions.get(source_id)
        return "free-football:competition:" + division if division else None

    def _append(self, entity, payload):
        with self.repository.engine.begin() as c:
            self.repository._append(c, "annotations", identifier="free-football:" + canonical_hash(payload),
                                    entity_id=entity, match_id=None, payload=payload)

    def save_rows(self, rows, url, acquired_at):
        groups = {}
        for row in rows:
            if row["division"] not in {"I1", "I2"}:
                raise ValueError("Unsupported division")
            key = (row["division"], row["season"])
            groups.setdefault(key, []).append(row)
        for (division, season), material in groups.items():
            identities = [(r["kickoff"], self.key(r["home"]), self.key(r["away"])) for r in material]
            if len(set(identities)) != len(identities):
                raise ValueError("Duplicate CSV/dataset fixtures")
            digest = canonical_hash({"rows": material, "source_url": url})
            content = {"rows": material, "source_url": url, "sha256": digest}
            entity = f"free-football:content:{digest}"
            self._append(entity, content)
            self._append(f"free-football:catalog:{division}:{season}:{url}", {
                "division": division, "season": season, "content": entity,
                "acquired_at": acquired_at.isoformat(), "source_url": url,
            })

    def rows(self, as_of, *, division=None, include_native=False):
        checks = [r for r in self.repository.all_records("annotations")
                  if r.get("content", "").startswith("free-football:content:")
                  and datetime.fromisoformat(r["acquired_at"]) <= as_of
                  and (division is None or r["division"] == division)]
        latest = {}
        for row in checks:
            key = (row["division"], row["season"], row["source_url"])
            if key not in latest or row["acquired_at"] > latest[key]["acquired_at"]:
                latest[key] = row
        result = []
        for check in latest.values():
            content = self.repository.all_records("annotations", entity_id=check["content"])[0]
            for row in content["rows"]:
                result.append({**row, "acquired_at": check["acquired_at"], "source_url": row.get("source_url", check["source_url"]),
                               "sha256": content["sha256"], "source_tier": "C"})
        if include_native and self.archived and self.archived.archive:
            for league, code in ((135, "I1"), (136, "I2")):
                if division is not None and code != division:
                    continue
                for season in self.settings.seasons:
                    catalog = self.archived.archive.latest("fixtures", {"league": league, "season": season}, as_of=as_of)
                    if not catalog:
                        continue
                    for row in catalog["data"]:
                        if row["fixture"]["status"]["short"] != "FT":
                            continue
                        score = row["score"]["fulltime"]
                        if any(type(score[s]) is not int for s in ("home", "away")):
                            continue
                        result.append({"division": code, "season": season,
                            "kickoff": datetime.fromisoformat(row["fixture"]["date"]).astimezone(UTC).isoformat(),
                            "home": row["teams"]["home"]["name"], "away": row["teams"]["away"]["name"],
                            "home_score": score["home"], "away_score": score["away"],
                            "statistics": {}, "source_tier": "B", "source_url": catalog["url"],
                            "acquired_at": catalog["retrieved_at"].isoformat(), "sha256": catalog["sha256"]})
        return result

    async def refresh_archive(self, *, matches=()):
        now = self.clock()
        checks = self.repository.all_records("annotations", entity_id="free-football:refresh")
        if checks and (now - datetime.fromisoformat(checks[-1]["at"])).total_seconds() < self.settings.refresh_seconds:
            return {"status": "CACHED", "requests": 0}
        requests, errors = 0, []
        for division in self.settings.openfootball_divisions:
            for season in self.settings.seasons:
                suffix = f"{season}-{str(season + 1)[-2:]}"
                url = f"https://raw.githubusercontent.com/openfootball/football.json/master/{suffix}/it.{1 if division == 'I1' else 2}.json"
                try:
                    response = await self.client.get(url)
                    requests += 1
                    if response.status_code == 404:
                        errors.append({"source": url, "status": "NOT_AVAILABLE"})
                        continue
                    response.raise_for_status()
                    self.save_rows(parse_openfootball(response.json(), division, season), url, self.clock())
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    errors.append({"source": url, "status": "ERROR", "type": type(error).__name__})
        for filename in self.settings.csv_files:
            try:
                path = Path(filename)
                self.save_rows(parse_csv(path.read_text(encoding="utf-8")), str(path.resolve()), self.clock())
            except (OSError, ValueError, KeyError) as error:
                errors.append({"source": filename, "status": "ERROR", "type": type(error).__name__})
        if self.football_data_org is not None:
            try:
                await self.football_data_org.refresh_standings(self.repository)
            except Exception as error:
                errors.append({"source": "football_data_org:standings", "status": "ERROR", "type": type(error).__name__})
        self.last_refresh = {"at": now.isoformat(), "requests": requests, "issues": errors}
        self._append("free-football:refresh", self.last_refresh)
        return {"status": "PARTIAL" if errors else "OK", **self.last_refresh}

    async def find_candidates(self, match, as_of):
        division = self.settings.competitions.get(match.competition)
        if not division:
            return ()
        rows = self.rows(as_of, division=division)
        found = []
        for row in rows:
            kickoff = datetime.fromisoformat(row["kickoff"])
            if abs((kickoff - match.kickoff).total_seconds()) > 15 * 60:
                continue
            home, away = self.key(row["home"]), self.key(row["away"])
            if home != self.key(match.home_team) or away != self.key(match.away_team):
                continue
            # Use verified aliases only; preserve the source names in evidence.
            found.append(ResultIdentityCandidate(
                provider=self.provider_id, source_tier="B", provider_match_id="free-football:match:" + canonical_hash({"date": row["kickoff"], "division": division, "home": home, "away": away}),
                competition_id=self.competition_target(match.competition), competition_name="Serie A" if division == "I1" else "Serie B",
                home_team_id="free-football:team:" + home, away_team_id="free-football:team:" + away,
                home_team_name=match.home_team, away_team_name=match.away_team, kickoff=kickoff,
                source_url=row["source_url"], evidence=row,
            ))
        # Same observation in multiple imports is one candidate; disagreements
        # remain separate and must not create a verified mapping.
        unique = {(r.provider_match_id, r.evidence["home_score"], r.evidence["away_score"]): r for r in found}
        return tuple(unique.values())

    async def _official_confirmation(self, row, as_of):
        if row["division"] != "I1":
            return None
        now = self.clock()
        if not self._official_index or now - self._official_index[0] > timedelta(hours=1):
            response = await self.client.get("https://www.legaseriea.it/serie-a/news")
            response.raise_for_status()
            links = set(re.findall(r"(?:https://www\.legaseriea\.it)?/serie-a/news/[a-z0-9-]+", response.text))
            self._official_index = (now, ["https://www.legaseriea.it" + x if x.startswith("/") else x for x in links])
        season = f"{row['season']}-{row['season'] + 1}"
        # Select by both oriented team names and season, not by expected score.
        urls = [u for u in self._official_index[1] if season in u and "cronaca-risultato-gol" in u
                and self.key(row["home"]).replace(" ", "-") in u and self.key(row["away"]).replace(" ", "-") in u]
        confirmations = []
        for url in urls:
            if url not in self._articles:
                response = await self.client.get(url)
                response.raise_for_status()
                self._articles[url] = response.text
            text = self._articles[url]
            for raw in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S):
                payload = json.loads(unescape(raw))
                for item in payload if isinstance(payload, list) else [payload]:
                    if item.get("@type") not in {"NewsArticle", "Article"}:
                        continue
                    published = datetime.fromisoformat(item["datePublished"].replace("Z", "+00:00"))
                    kickoff = datetime.fromisoformat(row["kickoff"])
                    if not kickoff + timedelta(minutes=90) <= published <= min(as_of, kickoff + timedelta(days=3)):
                        continue
                    title = unescape(item.get("headline", ""))
                    scored = re.match(r"^(.+?)\s*[-–]\s*(.+?)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})(?:\D|$)", title)
                    if scored and self.key(scored[1]) == self.key(row["home"]) and self.key(scored[2]) == self.key(row["away"]):
                        confirmations.append((int(scored[3]), int(scored[4]), url, published))
        if not confirmations:
            return None
        if len({x[:2] for x in confirmations}) != 1:
            raise ValueError("Conflicting official result reports")
        return confirmations[0]

    async def get_result(self, identity, as_of):
        if not isinstance(identity, ResultProviderIdentity) or identity.provider != self.provider_id:
            return None
        rows = [row for row in self.rows(as_of) if row["kickoff"] == identity.kickoff.isoformat()
                and self.key(row["home"]) == self.key(identity.home_team_name)
                and self.key(row["away"]) == self.key(identity.away_team_name)]
        if not rows:
            return None
        row = rows[0]
        official = await self._official_confirmation(row, as_of)
        if official is None:
            return None
        home, away, url, published = official
        conflict = row["home_score"] is not None and (home, away) != (row["home_score"], row["away_score"])
        return MatchResult(match_id=identity.source_match_id, final_status="CONFLICT" if conflict else "FINAL",
            home_score=None if conflict else home, away_score=None if conflict else away,
            source="Lega Serie A official report; fixture identity matched to local open observations",
            source_tier="A", source_url=url, published_at=published, retrieved_at=self.clock(),
            provider=self.provider_id, verified_at=as_of, provider_status="CONFLICT" if conflict else "FT")

    async def get_stats(self, match_id, as_of):
        # Existing API-Football evidence is read locally, never refreshed here.
        match = await self.fixtures.get_match(match_id)
        division = self.settings.competitions.get(match.competition)
        if not division:
            raise BetterError(ErrorCode.MISSING_DATA, "Competizione non operativa", source="local_archive")
        observations = self.rows(as_of, include_native=True)
        if not observations and self.archived:
            return await self.archived.get_stats(match_id, as_of)
        profiles, provenance, cutoffs = [], [], []
        for team_id, name in ((match.home_team_id, match.home_team), (match.away_team_id, match.away_team)):
            prior, evidence = {}, {}
            for row in observations:
                if any(type(row[s]) is not int for s in ("home_score", "away_score")):
                    continue
                date = datetime.fromisoformat(row["kickoff"])
                if not as_of - timedelta(days=400) <= date < min(as_of, match.kickoff):
                    continue
                if self.key(name) not in {self.key(row["home"]), self.key(row["away"])}:
                    continue
                home = self.key(name) == self.key(row["home"])
                opponent = row["away"] if home else row["home"]
                gf, ga = (row["home_score"], row["away_score"]) if home else (row["away_score"], row["home_score"])
                key = (date, self.key(opponent), home)
                if key in prior and (prior[key].goals_for, prior[key].goals_against) != (gf, ga):
                    raise BetterError(ErrorCode.SCHEMA_ERROR, "Conflitto tra risultati locali", source="local_archive")
                if key in evidence and evidence[key]["source_tier"] < row["source_tier"]:
                    continue
                prior[key] = RecentTeamMatch(fixture_id="free-football:observation:" + canonical_hash({"date": row["kickoff"], "home": self.key(row["home"]), "away": self.key(row["away"])}),
                    kickoff=date, competition_id=f"free-football:{row['division']}:{row['season']}", venue="HOME" if home else "AWAY",
                    opponent_id="free-football:team:" + self.key(opponent), opponent_name=opponent,
                    goals_for=gf, goals_against=ga, outcome="W" if gf > ga else "L" if gf < ga else "D")
                evidence[key] = row
            recent = sorted(prior.values(), key=lambda r: r.kickoff, reverse=True)[:10]
            if len(recent) < 2:
                if self.archived:
                    return await self.archived.get_stats(match_id, as_of)
                raise BetterError(ErrorCode.MISSING_DATA, "Archivio locale: campione insufficiente", source="local_archive")
            from better.providers.api_football import APIFootballProvider
            used = {r.fixture_id for r in recent}
            for key, r in prior.items():
                if r.fixture_id in used or r.competition_id == f"free-football:{division}:{season_year(match.kickoff)}":
                    row = evidence[key]
                    provenance.append(Provenance(source_id="local_football_archive", source_type=SourceType.MEDIA,
                        reliability_tier=ReliabilityTier(row["source_tier"]), url=row["source_url"], retrieved_at=datetime.fromisoformat(row["acquired_at"]),
                        field="recent_results", raw_reference="sha256:" + row["sha256"]))
            segments = {k: v for k, v in {
                "last_5": APIFootballProvider._segment(recent[:5]), "last_10": APIFootballProvider._segment(recent),
                "season": APIFootballProvider._segment([r for r in prior.values() if r.competition_id == f"free-football:{division}:{season_year(match.kickoff)}"]),
                "home": APIFootballProvider._segment([r for r in recent if r.venue == "HOME"]),
                "away": APIFootballProvider._segment([r for r in recent if r.venue == "AWAY"]),
            }.items() if v is not None}
            profiles.append(TeamStats(team_id=team_id, sample_size=len(recent), goals_for_per_match=sum(r.goals_for for r in recent) / len(recent),
                                      goals_against_per_match=sum(r.goals_against for r in recent) / len(recent), segments=segments, recent_matches=tuple(recent)))
            cutoffs.append(recent[0].kickoff)
        sources = tuple(dict.fromkeys(provenance))
        return StatsSnapshot(snapshot_id="free-football:stats:" + canonical_hash([p.model_dump(mode="json") for p in profiles]),
                             match_id=match_id, home=profiles[0], away=profiles[1], data_cutoff=max(cutoffs),
                             captured_at=max(p.retrieved_at for p in sources), provenance=sources)

    async def get_lineups(self, match_id, as_of):
        if self.archived:
            return await self.archived.get_lineups(match_id, as_of)
        raise BetterError(ErrorCode.MISSING_DATA, "Formazioni non presenti nell'archivio; ricerca ufficiale necessaria", source="local_archive")

    async def close(self):
        await self.client.aclose()
        if self.archived:
            await self.archived.close()
