"""Local provider evidence for configured leagues. Never writes live validation records."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from better.observability.diagnostics import failure_detail
from better.storage.db import TABLES
from better.storage.repository import canonical_hash

LEAGUE = 135
DETAILS = {
    "fixtures/events": "events",
    "fixtures/lineups": "lineups",
    "fixtures/statistics": "statistics",
    "fixtures/players": "players",
}


def instant(value):
    return datetime.fromisoformat(value).astimezone(UTC)


class FootballArchive:
    def __init__(self, repository, seasons=(2026, 2025, 2024), leagues=(135,)):
        self.repository = repository
        self.seasons = tuple(seasons)
        self.leagues = tuple(dict.fromkeys(leagues))

    def pairs(self):
        return [(league, season) for season in self.seasons for league in self.leagues]

    @staticmethod
    def key(endpoint, params):
        return canonical_hash({"endpoint": endpoint, "params": {k: str(v) for k, v in params.items()}})

    def latest(self, endpoint, params, *, as_of=None):
        checks, contents = TABLES["api_football_archive_checks"], TABLES["api_football_archive"]
        with self.repository.engine.connect() as connection:
            rows = connection.execute(
                select(checks.c.payload)
                .where(checks.c.entity_id == self.key(endpoint, params))
                .order_by(checks.c.recorded_at.desc(), checks.c.id.desc())
            ).scalars()
            eligible = [r for r in rows if as_of is None or instant(r["checked_at"]) <= as_of]
            if not eligible:
                return None
            check = max(eligible, key=lambda r: r["checked_at"])
            content = connection.execute(
                select(contents.c.payload).where(contents.c.id == check["content_id"])
            ).scalar_one()
        return {
            "data": content["data"],
            "retrieved_at": instant(check["checked_at"]),
            "first_acquired_at": instant(content["first_acquired_at"]),
            "url": check["url"],
            "sha256": content["data_hash"],
            "expires_at": instant(check["expires_at"]),
            "archive_hit": True,
            "detail_checked": check.get("detail_checked", False),
        }

    def lookup(self, endpoint, params, now, ttl):
        """Only fresh evidence acquired by now; no future data in an as-of lookup."""

        def fresh(row):
            stable = (
                row
                and row.get("detail_checked")
                and row["data"]
                and all(
                    item.get("fixture", {}).get("status", {}).get("short") == "FT" for item in row["data"]
                )
            )
            return (
                row
                and now < row["expires_at"]
                and (stable or (now - row["retrieved_at"]).total_seconds() < ttl)
            )

        exact = self.latest(endpoint, params, as_of=now)
        if fresh(exact):
            return exact
        # Rich fixture documents can serve all four detail endpoints locally.
        if endpoint in DETAILS and set(params) == {"fixture"}:
            document = self.latest("fixtures", {"id": params["fixture"]}, as_of=now)
            if fresh(document) and document["data"] and DETAILS[endpoint] in document["data"][0]:
                return {**document, "data": document["data"][0][DETAILS[endpoint]]}
        # A complete season catalog answers team/date queries without another API call.
        if endpoint != "fixtures" or not set(params) <= {"league", "season", "team", "status", "date", "id"}:
            return None
        if "id" not in params and not {"league", "season"} <= set(params):
            return None
        for league, season in self.pairs():
            if "season" in params and str(season) != str(params["season"]):
                continue
            if "league" in params and str(params["league"]) != str(league):
                continue
            catalog = self.latest("fixtures", {"league": league, "season": season}, as_of=now)
            if not fresh(catalog):
                continue
            rows = catalog["data"]
            if "id" in params:
                rows = [r for r in rows if str(r["fixture"]["id"]) == str(params["id"])]
                if not rows:
                    continue
            if "team" in params:
                rows = [
                    r
                    for r in rows
                    if str(params["team"]) in {str(r["teams"][s]["id"]) for s in ("home", "away")}
                ]
            if "status" in params:
                rows = [
                    r for r in rows if r["fixture"]["status"]["short"] in str(params["status"]).split("-")
                ]
            if "date" in params:
                rows = [
                    r for r in rows if instant(r["fixture"]["date"]).date().isoformat() == str(params["date"])
                ]
            return {**catalog, "data": rows}
        return None

    def save(self, endpoint, params, response, ttl):
        data = response["data"]
        if endpoint == "fixtures":
            if (
                not isinstance(data, list)
                or not data
                or any(
                    r.get("league", {}).get("id") not in self.leagues
                    or r.get("league", {}).get("season") not in self.seasons
                    for r in data
                )
            ):
                return
            # Bulk detail responses are split once; no redundant bulk payload is retained.
            if "ids" in params:
                for row in data:
                    self._save(
                        "fixtures",
                        {"id": row["fixture"]["id"]},
                        {**response, "data": [row], "detail_checked": True},
                        ttl,
                    )
                return
        elif endpoint == "leagues":
            if int(params.get("id", 0)) not in self.leagues or int(params.get("season", 0)) not in self.seasons:
                return
        elif endpoint == "standings":
            if int(params.get("league", 0)) not in self.leagues or int(params.get("season", 0)) not in self.seasons:
                return
        elif endpoint in DETAILS:
            if not self.fixture(params.get("fixture")):
                return
        else:
            return
        self._save(endpoint, params, response, ttl)

    def _save(self, endpoint, params, response, ttl):
        key = self.key(endpoint, params)
        data_hash = canonical_hash(response["data"])
        identifier = canonical_hash({"key": key, "data": data_hash})
        now = response["retrieved_at"].astimezone(UTC)
        content = {
            "endpoint": endpoint,
            "params": params,
            "data": response["data"],
            "data_hash": data_hash,
            "first_acquired_at": now.isoformat(),
            "provider": "api_football",
            "source_tier": "B",
            "purpose": "HISTORICAL_ARCHIVE",
        }
        check = {
            "content_id": identifier,
            "checked_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "url": response["url"],
            "raw_sha256": response["sha256"],
            "detail_checked": response.get("detail_checked", False),
        }
        with self.repository.engine.begin() as connection:
            table = TABLES["api_football_archive"]
            if (
                connection.execute(select(table.c.id).where(table.c.id == identifier)).scalar_one_or_none()
                is None
            ):
                self.repository._append(
                    connection,
                    "api_football_archive",
                    identifier=identifier,
                    entity_id=key,
                    match_id=None,
                    payload=content,
                )
            self.repository._append(
                connection,
                "api_football_archive_checks",
                identifier=str(uuid4()),
                entity_id=key,
                match_id=None,
                payload=check,
            )

    def fixture(self, fixture_id, *, as_of=None):
        if fixture_id is None:
            return None
        exact = self.latest("fixtures", {"id": fixture_id}, as_of=as_of)
        if exact:
            return exact
        for league, season in self.pairs():
            catalog = self.latest("fixtures", {"league": league, "season": season}, as_of=as_of)
            if catalog:
                rows = [r for r in catalog["data"] if str(r["fixture"]["id"]) == str(fixture_id)]
                if rows:
                    return {**catalog, "data": rows}
        return None

    def reserve_request(self, now, limit, endpoint, params):
        """Durable budget across restarts; count failed/cancelled network attempts too."""
        table = TABLES["api_football_archive_attempts"]
        day = now.astimezone(UTC).date().isoformat()
        with self.repository.engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(72819433)")
            count = connection.execute(
                select(func.count()).select_from(table).where(table.c.entity_id == day)
            ).scalar_one()
            if count >= limit:
                connection.rollback()
                return False
            self.repository._append(
                connection,
                "api_football_archive_attempts",
                identifier=str(uuid4()),
                entity_id=day,
                match_id=None,
                payload={"at": now.isoformat(), "endpoint": endpoint, "params": params},
            )
            connection.commit()
        return True

    def report(self, now, league=LEAGUE):
        rows = []
        for season in self.seasons:
            catalog = self.latest("fixtures", {"league": league, "season": season})
            matches = catalog["data"] if catalog else []
            finished = [r for r in matches if r["fixture"]["status"]["short"] == "FT"]
            counts = {field: 0 for field in DETAILS.values()}
            detailed = 0
            for row in finished:
                saved = self.latest("fixtures", {"id": row["fixture"]["id"]})
                if saved and saved["detail_checked"]:
                    detailed += 1
                    for field in counts:
                        counts[field] += bool(saved["data"][0].get(field))
            coverage = self.latest("leagues", {"id": league, "season": season})
            rows.append(
                {
                    "season": season,
                    "fixtures": len(matches),
                    "finished": len(finished),
                    "detailed": detailed,
                    "populated": counts,
                    "standings": bool(
                        (self.latest("standings", {"league": league, "season": season}) or {}).get("data")
                    ),
                    "coverage": coverage["data"] if coverage else [],
                    "last_catalog_check": catalog["retrieved_at"].isoformat() if catalog else None,
                }
            )
        with self.repository.engine.connect() as connection:
            table = TABLES["api_football_archive"]
            versions = connection.execute(select(func.count()).select_from(table)).scalar_one()
            attempts = TABLES["api_football_archive_attempts"]
            used = connection.execute(
                select(func.count())
                .select_from(attempts)
                .where(attempts.c.entity_id == now.astimezone(UTC).date().isoformat())
            ).scalar_one()
        return {
            "league": league,
            "seasons": rows,
            "content_versions": versions,
            "backfill_requests_today": used,
            "live_forward_observations_created": 0,
            "last_run": self.repository.list_records(
                "annotations", last=1, entity_id="api-football:archive:run"
            ),
        }


async def collect_archive(provider, *, matches=()):
    async with provider.archive_lock:
        result = await _collect_archive(provider, matches=matches)
        if provider.archive and (result.get("requests") or result["status"] == "ERROR"):
            with provider.repository.engine.begin() as connection:
                provider.repository._append(
                    connection,
                    "annotations",
                    identifier=str(uuid4()),
                    entity_id="api-football:archive:run",
                    match_id=None,
                    payload={**result, "at": provider.clock().isoformat()},
                )
        return result


async def _collect_archive(provider, *, matches=()):
    """Bounded cache refresh prioritizing inputs for upcoming checkpoints."""
    archive, settings = provider.archive, provider.settings
    if archive is None or not settings.archive_enabled:
        return {"status": "DISABLED"}
    calls = 0
    pairs = archive.pairs()
    previous = provider.repository.list_records("annotations", last=1, entity_id="api-football:archive:run")
    cursor = (previous[0].get("next_pair", 0) if previous else 0) % len(pairs)
    next_pair = cursor
    order = [(cursor + offset) % len(pairs) for offset in range(len(pairs))]
    urgent = []
    for index in order:
        league_id, season = pairs[index]
        coverage = archive.latest("leagues", {"id": league_id, "season": season})
        active = coverage and any(
            s.get("current") for league in coverage["data"] for s in league.get("seasons", [])
            if s.get("year") == season
        )
        if active and not archive.lookup("fixtures", {"league": league_id, "season": season}, provider.clock(), 3600):
            urgent.append(index)
    order = urgent + [index for index in order if index not in urgent]
    targets = {}
    for match in sorted(matches, key=lambda m: m.kickoff):
        binding = settings.competitions.get(match.competition)
        if binding and match.kickoff > provider.clock():
            targets.setdefault((binding.league, binding.season), []).append(match)
    priority = [pairs.index(pair) for pair in targets if pair in pairs]
    order = priority + [index for index in order if index not in priority]

    async def obtain(endpoint, params, ttl):
        nonlocal calls
        cached = archive.lookup(endpoint, params, provider.clock(), ttl)
        if cached:
            return cached
        if calls >= settings.archive_batch_requests:
            return None
        calls += 1
        return await provider.request(endpoint, params, ttl=ttl, backfill=True)

    try:
        for index in order:
            league_id, season = pairs[index]
            next_pair = index
            coverage = await obtain("leagues", {"id": league_id, "season": season}, 7 * 86400)
            if coverage is None:
                break
            seasons = [
                s
                for league in coverage["data"]
                if league["league"]["id"] == league_id
                for s in league["seasons"]
                if s["year"] == season
            ]
            if not seasons:
                next_pair = (index + 1) % len(pairs)
                continue
            active = seasons[0].get("current", False)
            catalog = await obtain(
                "fixtures", {"league": league_id, "season": season}, 3600 if active else 30 * 86400
            )
            if catalog is None:
                break
            if seasons[0].get("coverage", {}).get("standings"):
                if (
                    await obtain(
                        "standings", {"league": league_id, "season": season}, 6 * 3600 if active else 30 * 86400
                    )
                    is None
                ):
                    break
            pending = []
            needed = set()
            if (league_id, season) in targets:
                from better.providers.result_identity import normalize_team_name

                for match in targets[(league_id, season)]:
                    matching = [row for row in catalog["data"]
                                if instant(row["fixture"]["date"]) == match.kickoff
                                and normalize_team_name(row["teams"]["home"].get("name", "")) == normalize_team_name(match.home_team)
                                and normalize_team_name(row["teams"]["away"].get("name", "")) == normalize_team_name(match.away_team)]
                    if len(matching) != 1:
                        continue
                    for side in ("home", "away"):
                        team = matching[0]["teams"][side]["id"]
                        history = [row for row in catalog["data"]
                                   if row["fixture"]["status"]["short"] == "FT"
                                   and instant(row["fixture"]["date"]) < min(provider.clock(), match.kickoff)
                                   and team in (row["teams"]["home"]["id"], row["teams"]["away"]["id"])]
                        history.sort(key=lambda row: row["fixture"]["date"], reverse=True)
                        needed.update(row["fixture"]["id"] for row in history[:settings.sample_matches])
            for row in sorted(catalog["data"], key=lambda r: r["fixture"]["date"], reverse=True):
                if row["league"]["id"] != league_id or row["league"]["season"] != season:
                    raise ValueError("Unexpected league/season in catalog")
                score = row.get("score", {}).get("fulltime", {})
                if row["fixture"]["status"]["short"] != "FT" or any(
                    type(score.get(side)) is not int for side in ("home", "away")
                ):
                    continue
                if instant(row["fixture"]["date"]) >= provider.clock():
                    continue
                saved = archive.latest("fixtures", {"id": row["fixture"]["id"]})
                if not saved or saved["expires_at"] <= provider.clock() or not saved["detail_checked"]:
                    pending.append(row["fixture"]["id"])
            # One detail batch per season per cycle avoids starving older seasons.
            if pending and calls < settings.archive_batch_requests:
                pending.sort(key=lambda identifier: identifier not in needed)
                ids = pending[:20]
                # Recent finals get an early correction check; older matches are rechecked weekly.
                ttl = (
                    6 * 3600
                    if any(
                        instant(r["fixture"]["date"]) > provider.clock() - timedelta(days=2)
                        for r in catalog["data"]
                        if r["fixture"]["id"] in ids
                    )
                    else 7 * 86400
                )
                await obtain("fixtures", {"ids": "-".join(map(str, ids))}, ttl)
            next_pair = (index + 1) % len(pairs)
            if calls >= settings.archive_batch_requests:
                break
        return {"status": "OK", "requests": calls, "next_pair": next_pair}
    except Exception as error:
        # Never persist arbitrary provider error text or credentials.
        return {
            "status": "ERROR",
            "requests": calls,
            "error": failure_detail(error, provider="api_football", stage="ARCHIVE_REFRESH"),
            "next_pair": (next_pair + 1) % len(pairs),
            "reason_code": str(getattr(getattr(error, "detail", None), "code", "ARCHIVE_FAILED")),
        }
