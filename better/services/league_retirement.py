"""Explicit, offline retirement of Serie B after a complete verified backup.

Financial and API cost ledgers are never removed. Shared evidence for current
Serie A teams remains available for rolling samples across a promotion.
"""

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from better.services.data_backup import create_backup, digest
from better.services.league_configuration import SERIE_B

PRESERVED_TABLES = frozenset({
    "validation_cost_events", "api_football_archive_attempts", "bet_candidates",
    "bet_slips", "bet_slip_selections", "bet_notes", "bet_events",
})


def retired_match_ids(connection):
    matches = set()
    for table in ("matches", "validation_cohort", "canonical_predictions", "historical_observations"):
        for payload, in connection.execute('SELECT payload FROM "' + table + '"'):
            row = json.loads(payload)
            match = row.get("match", row)
            if match.get("competition", row.get("competition_id")) == SERIE_B:
                matches.add(match.get("match_id", row.get("match_id")))
    return matches - {None}


def purge_retired_records(connection):
    """Caller owns the exclusive backend lock, verified backup and transaction."""
    matches = retired_match_ids(connection)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    candidates = tables - PRESERVED_TABLES - {"alembic_version"}
    removed = {}
    triggers = [(name, sql) for name, table, sql in connection.execute(
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'") if table in candidates]
    for name, _ in triggers:
        connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    try:
        for table in sorted(candidates):
            columns = {r[1] for r in connection.execute('PRAGMA table_info("' + table + '")')}
            if not {"id", "match_id", "payload"} <= columns:
                continue
            identifiers = [(r[0],) for r in connection.execute('SELECT id,match_id,payload FROM "' + table + '"')
                           if r[1] in matches and not (table == "annotations" and
                               json.loads(r[2]).get("reservation_id"))]
            connection.executemany('DELETE FROM "' + table + '" WHERE id=?', identifiers)
            if identifiers:
                removed[table] = len(identifiers)
        # Retain detailed native fixtures involving today's Serie A teams. The
        # full Serie B catalogs stop being an operational data source.
        a_teams = set()
        contents = []
        for identifier, payload in connection.execute('SELECT id,payload FROM api_football_archive'):
            row = json.loads(payload)
            contents.append((identifier, row))
            if row["endpoint"] == "fixtures" and str(row["params"].get("league")) == "135" and str(row["params"].get("season")) == "2026":
                a_teams.update(t["id"] for r in row["data"] for t in r["teams"].values() if isinstance(t, dict) and "id" in t)
        stale = []
        for identifier, row in contents:
            league = str(row["params"].get("league", row["params"].get("id") if row["endpoint"] == "leagues" else ""))
            native_b = [r for r in row["data"] if isinstance(r, dict) and r.get("league", {}).get("id") == 136]
            is_b = league == "136" or (native_b and len(native_b) == len(row["data"]))
            individual = row["endpoint"] == "fixtures" and set(row["params"]) == {"id"}
            shared = individual and any(t.get("id") in a_teams for r in native_b for t in r.get("teams", {}).values() if isinstance(t, dict))
            if is_b and not shared:
                stale.append((identifier,))
        for identifier, in stale:
            connection.execute("DELETE FROM api_football_archive_checks WHERE json_extract(payload,'$.content_id')=?", (identifier,))
        connection.executemany('DELETE FROM api_football_archive WHERE id=?', stale)
        removed["api_football_archive"] = removed.get("api_football_archive", 0) + len(stale)
    finally:
        for _, sql in triggers:
            connection.execute(sql)
    return {"matches_removed": len(matches), "removed": removed}


def retire_serie_b(home, *, backend_lock, safety_directory):
    home = Path(home).resolve()
    identity = hashlib.sha256(str(home).lower().encode()).hexdigest()[:24]
    if (not backend_lock.acquired or getattr(backend_lock, "account_identity", None) != identity
            or getattr(backend_lock, "kind", None) != "backend"):
        raise ValueError("Rimozione consentita soltanto con il blocco backend della stessa installazione")
    app = yaml.safe_load((home / "config/888/app.yaml").read_text(encoding="utf-8"))
    url = app["database_url"]
    if not url.startswith("sqlite:///data/"):
        raise ValueError("Database non locale")
    path = (home / url.removeprefix("sqlite:///")).resolve()
    if not path.is_relative_to(home / "data") or not path.is_file():
        raise ValueError("Percorso database non consentito")
    backup = create_backup(home)
    safety = Path(safety_directory).resolve()
    safety.mkdir(parents=True, exist_ok=True)
    saved = safety / backup.name
    shutil.copy2(backup, saved)
    if digest(backup) != digest(saved):
        raise ValueError("Copia di sicurezza non verificata")
    # Preserve the rolling observations of promoted Serie A teams, with the
    # ORIGINAL source tier and acquisition timestamps (never today's timestamp).
    from better.providers.api_football import APIFootballProvider, APIFootballSettings
    from better.providers.free_football import FreeFootballProvider, FreeFootballSettings
    from better.storage.repository import Repository
    sources = yaml.safe_load((home / "config/888/sources.yaml").read_text(encoding="utf-8"))
    repository = Repository("sqlite:///" + path.as_posix())
    archived = APIFootballProvider(APIFootballSettings(archive_enabled=True, analysis_archive_only=True,
        network_enabled=False, archive_leagues=(135, 136)), repository=repository, key="")
    free = FreeFootballProvider(FreeFootballSettings.model_validate(sources["free_football"]),
        repository=repository, archived=archived)
    now = datetime.now(UTC)
    observed = free.rows(now, include_native=True)
    names = {free.key(r[side]) for r in observed if r["division"] == "I1" and r["season"] == 2026
             for side in ("home", "away")}
    groups = {}
    for row in observed:
        if (row["division"] != "I2" or not names.intersection((free.key(row["home"]), free.key(row["away"])))
                or datetime.fromisoformat(row["kickoff"]) < now - timedelta(days=400)):
            continue
        key = row["source_url"], row["acquired_at"], row["source_tier"]
        material = {k: row[k] for k in ("division", "season", "kickoff", "home", "away", "home_score", "away_score", "statistics")}
        groups.setdefault(key, {})[(row["kickoff"], row["home"], row["away"])] = material
    try:
        for (url, acquired, tier), material in groups.items():
            free.save_rows(list(material.values()), url, datetime.fromisoformat(acquired), source_tier=tier)
    finally:
        import asyncio
        asyncio.run(free.close())
        repository.close()
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Database non integro")
        report = purge_retired_records(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Riferimenti incoerenti; rimozione annullata")
    return {**report, "shared_observations": sum(len(r) for r in groups.values()),
            "database": str(path), "safety_backup": str(saved)}
