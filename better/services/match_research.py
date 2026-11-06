"""On-demand source collection for one selected match. No agents or betting decision."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.storage.repository import canonical_hash
from better.system_zero.freshness import freshness


class MatchResearchService:
    def __init__(self, runtime, clock=None):
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cache = {}
        self._lock = asyncio.Lock()

    async def inspect(self, match_id):
        # One shared collector at a time; repeated clicks do not fan out provider requests.
        async with self._lock:
            now = self.clock()
            cached = self._cache.get(match_id)
            if cached and (now - cached[0]).total_seconds() < 15:
                return self._report(*cached[1], checked_at=now, cached=True)
            match = await self.runtime.providers.fixtures.get_match(match_id)
            if match.match_id != match_id:
                raise BetterError(ErrorCode.SCHEMA_ERROR, "Identita partita non coerente")
            if now >= match.kickoff:
                raise BetterError(
                    ErrorCode.MISSING_DATA, "Consultazione pre-match non disponibile dopo il kickoff"
                )
            _, snapshots, failures = await self.runtime.collector.collect(match, now, live=True)
            research_match = match
            fixture_provider = self.runtime.providers.fixtures
            if hasattr(fixture_provider, "competition_names"):
                competition_name = fixture_provider.competition_names().get(match.competition)
                if competition_name:
                    research_match = match.model_copy(update={"competition": competition_name})
            collected_at = self.clock()
            if collected_at >= match.kickoff:
                raise BetterError(ErrorCode.MISSING_DATA, "Raccolta completata dopo il kickoff")
            external = None
            enriched = None
            football_data_result = None
            football_data_service = getattr(self.runtime, "football_data_research", None)
            from better.services.football_data_research import (
                merge_football_stats,
                missing_model_research_fields,
            )
            missing_stats = missing_model_research_fields(snapshots.get("stats"), self.runtime.settings.min_training_sample)
            if football_data_service is not None and missing_stats:
                from better.services.football_data_research import normalize_football_data

                football_data_result = await football_data_service.research(research_match, now, requested_fields=missing_stats)
                stats = merge_football_stats(snapshots.get("stats"), normalize_football_data(match, football_data_result))
                if stats is not None:
                    snapshots["stats"] = stats
                    failures.pop("stats", None)
            external_service = getattr(self.runtime, "external_research", None)
            if external_service is not None:
                if external_service.settings.research_primary_source:
                    missing = tuple(
                        name for name, policy in external_service.settings.fields.items() if policy.enabled
                    )
                    # Odds are collected independently by 888 and may change every
                    # few seconds. They must not invalidate qualitative research.
                    from better.services.external_research import qualitative_source_state

                    state = qualitative_source_state(match)
                else:
                    from better.services.external_research import (
                        missing_qualitative_fields,
                        qualitative_source_state,
                    )
                    missing = missing_qualitative_fields(snapshots, collected_at, external_service.settings,
                                                         news_ttl=self.runtime.settings.news_ttl_seconds, kickoff=match.kickoff)
                    state = qualitative_source_state(match)
                external = await external_service.research_missing(
                    research_match,
                    as_of=now,
                    missing_fields=missing,
                    source_state_version=state,
                )
                if external is not None:
                    from better.services.research_enrichment import (
                        build_enriched_snapshot,
                        normalized_qualitative_snapshots,
                    )

                    enriched = build_enriched_snapshot(
                        match,
                        external,
                        football_result=football_data_result,
                        odds=snapshots.get("odds"),
                    )
                    for name, snapshot in normalized_qualitative_snapshots(match, enriched).items():
                        snapshots[name] = snapshot
                        failures.pop(name, None)
                collected_at = self.clock()
                if collected_at >= match.kickoff:
                    raise BetterError(ErrorCode.MISSING_DATA, "Raccolta completata dopo il kickoff")
            if enriched is None and football_data_result is not None:
                from better.services.research_enrichment import build_enriched_snapshot

                enriched = build_enriched_snapshot(
                    match,
                    None,
                    football_result=football_data_result,
                    odds=snapshots.get("odds"),
                )
            collected_at = self.clock()
            if collected_at >= match.kickoff:
                raise BetterError(ErrorCode.MISSING_DATA, "Raccolta completata dopo il kickoff")
            material = (
                match,
                snapshots,
                failures,
                collected_at,
                external,
                enriched,
                football_data_result,
            )
            report = self._report(*material, checked_at=collected_at, cached=False)
            document = {
                "report": report,
                "snapshots": {k: v.model_dump(mode="json") for k, v in snapshots.items()},
                "failures": {k: e.detail.model_dump(mode="json") for k, e in failures.items()},
                "enriched_snapshot": enriched.model_dump(mode="json") if enriched else None,
                "football_data_research": football_data_result.model_dump(mode="json")
                if football_data_result
                else None,
            }
            repository = self.runtime.repository
            from better.storage.repository import Repository

            if isinstance(repository, Repository) and match.match_id.startswith("888it:match:"):
                from better.services.data_pipeline import checkpoint_for

                repository.select_for_collection(match=match, selected_at=now)
                lineup = snapshots.get("lineups")
                checkpoint = checkpoint_for(
                    match.kickoff,
                    collected_at,
                    official_lineup=bool(lineup and lineup.status == "CONFIRMED_LINEUP"),
                )
                if not repository.has_historical_checkpoint(match.match_id, checkpoint):
                    repository.archive_pre_match_observation(
                        match=match,
                        as_of=now,
                        collected_at=collected_at,
                        snapshots=snapshots,
                        failures=failures,
                        purpose="SELECTED_MATCH_RESEARCH",
                        checkpoint=checkpoint,
                    )
            with repository.engine.begin() as connection:
                repository._append(
                    connection,
                    "annotations",
                    identifier="research:" + canonical_hash(document),
                    entity_id="match-research:" + match_id,
                    match_id=match_id,
                    payload=document,
                )
            if len(self._cache) >= 50:
                self._cache.pop(next(iter(self._cache)))
            self._cache[match_id] = (collected_at, material)
            return report

    def _researchable_missing(self, snapshots, failures, checked_at):
        if "news" in failures or "news" not in snapshots:
            return ("news",)
        news = snapshots["news"]
        quality = freshness(
            news.captured_at,
            checked_at,
            timedelta(seconds=self.runtime.settings.news_ttl_seconds),
        )
        return () if quality.fresh else ("news",)

    def _report(
        self,
        match,
        snapshots,
        failures,
        collected_at,
        external=None,
        enriched=None,
        football_data_result=None,
        *,
        checked_at,
        cached,
    ):
        from better.agents.data_analyst import REQUIRED_SEGMENTS

        settings = self.runtime.settings
        sections = []
        for name, ttl in (
            ("stats", settings.stats_ttl_seconds),
            ("odds", settings.odds_ttl_seconds),
            ("news", settings.news_ttl_seconds),
            ("lineups", settings.lineups_ttl_seconds),
        ):
            row = {
                "kind": name,
                "available": name in snapshots,
                "complete": False,
                "fresh": None,
                "age_seconds": None,
                "ttl_seconds": ttl,
                "reliability_tiers": [],
                "sources": [],
                "missing": [],
                "consistency": "NOT_VERIFIED",
                "consistency_reason": "Confronto indipendente non disponibile",
            }
            if name in failures:
                row["error"] = failures[name].detail.model_dump(mode="json")
                if "SOURCE_CONFLICT" in row["error"]["message"]:
                    row.update(consistency="CONFLICT", consistency_reason="Disaccordo materiale tra fonti")
                sections.append(row)
                continue
            snapshot = snapshots.get(name)
            if snapshot is None:
                sections.append(row)
                continue
            if name == "odds":
                provenance = [q.provenance for q in snapshot.quotes]
                observed = min((q.captured_at for q in snapshot.quotes), default=None)
                row["quote_count"] = len(snapshot.quotes)
                selections = {
                    "1X2": {"HOME", "DRAW", "AWAY"},
                    "OVER_UNDER_2_5": {"OVER_2_5", "UNDER_2_5"},
                    "BTTS": {"YES", "NO"},
                }
                grouped = {}
                for quote in snapshot.quotes:
                    grouped.setdefault(
                        (quote.bookmaker, quote.market.type.value, quote.captured_at), set()
                    ).add(quote.market.selection)
                row["complete_markets"] = sorted(
                    {
                        market
                        for (_, market, _), outcomes in grouped.items()
                        if outcomes == selections.get(market)
                    }
                )
                row["missing"] = [
                    market for market in settings.supported_markets if market not in row["complete_markets"]
                ]
            else:
                provenance = snapshot.provenance
                observed = snapshot.captured_at
                if name == "lineups" and snapshot.status == "CONFIRMED_LINEUP":
                    ttl = settings.official_lineups_ttl_seconds
                    row["ttl_seconds"] = ttl
                if name == "stats":
                    for side in ("home", "away"):
                        team = getattr(snapshot, side)
                        row["missing"].extend(
                            f"{side}.{s}" for s in sorted(REQUIRED_SEGMENTS - set(team.segments))
                        )
                        row["missing"].extend(
                            f"{side}.{field}"
                            for field in ("xg_per_match", "xga_per_match")
                            if getattr(team, field) is None
                        )
                        if team.sample_size < settings.min_training_sample:
                            row["missing"].append(f"{side}.sample_size")
                    if (
                        snapshot.home.team_id != match.home_team_id
                        or snapshot.away.team_id != match.away_team_id
                    ):
                        row["missing"].append("team_identity")
                    row["data_cutoff"] = snapshot.data_cutoff.isoformat()
                    row["sample_sizes"] = {
                        "home": snapshot.home.sample_size,
                        "away": snapshot.away.sample_size,
                    }
                if name == "lineups" and snapshot.status != "CONFIRMED_LINEUP":
                    row["missing"].append("official_lineups")
                if name == "news":
                    row["item_count"] = len(snapshot.items)
            if observed:
                quality = freshness(observed, checked_at, timedelta(seconds=ttl))
                row.update(
                    fresh=quality.fresh,
                    age_seconds=round(quality.age_seconds, 1),
                    captured_at=observed.isoformat(),
                )
            else:
                row["available"] = False
            row["complete"] = row["available"] and not row["missing"]
            row["reliability_tiers"] = sorted({p.reliability_tier.value for p in provenance})
            row["sources"] = list(
                {
                    (p.source_id, p.url): {
                        "source_id": p.source_id,
                        "primary_source_id": p.primary_source_id,
                        "url": p.url,
                        "retrieved_at": p.retrieved_at.isoformat(),
                        "tier": p.reliability_tier.value,
                    }
                    for p in provenance
                }.values()
            )
            # Distinct labels alone do not establish independent corroboration.
            provider = getattr(self.runtime.providers, name)
            records = getattr(provider, "comparison_records", [])
            matching = [
                r
                for r in records
                if r.get("operation") == f"get_{name}"
                and isinstance(r.get("primary"), dict)
                and r["primary"].get("match_id") == match.match_id
                and canonical_hash(r["primary"]) == canonical_hash(snapshot.model_dump(mode="json"))
            ]
            if matching and matching[-1].get("action") == "PRIMARY_WITH_COMPARISON":
                comparison = matching[-1]
                roots = []
                for side in ("primary", "secondary"):
                    data = comparison[side]
                    refs = (
                        data.get("provenance", [])
                        if name != "odds"
                        else [q["provenance"] for q in data.get("quotes", [])]
                    )
                    roots.append({p.get("primary_source_id") for p in refs if p.get("primary_source_id")})
                if all(roots) and roots[0].isdisjoint(roots[1]):
                    row.update(
                        consistency="CONSISTENT",
                        consistency_reason="Confronto effettuato entro le tolleranze configurate",
                    )
            sections.append(row)
        if football_data_result is not None:
            stats_row = next(row for row in sections if row["kind"] == "stats")
            stats_row["research_found_fields"] = [
                finding.field for finding in football_data_result.findings if finding.status.value == "FOUND"
            ]
            stats_row["research_unresolved_fields"] = [
                finding.field for finding in football_data_result.findings if finding.status.value != "FOUND"
            ]
            stats_row["normalization_reason"] = (
                None
                if stats_row["available"]
                else "Il modello richiede risultati partita-per-partita verificati per entrambe le squadre; "
                "le metriche aggregate trovate restano evidenze consultabili ma non vengono trasformate "
                "in probabilita."
            )
        history = [
            h
            for h in self.runtime.providers.validation_history
            if h.competition == match.competition and h.settled_at <= checked_at
        ]
        report = {
            "match_id": match.match_id,
            "home_team": match.home_team,
            "away_team": match.away_team,
            "collected_at": collected_at.isoformat(),
            "checked_at": checked_at.isoformat(),
            "cached": cached,
            "sections": sections,
            "history": {
                "settled_predictions": len(history),
                "minimum_sample": settings.min_sample,
                "counts_by_model_market": dict(
                    Counter(f"{h.model_id}:{h.model_version}:{h.market}" for h in history)
                ),
                "note": "Solo previsioni concluse della stessa competizione; la numerosita non certifica la validazione.",
            },
            "analysis_started": False,
            "note": "Raccolta automatica della partita selezionata. I gate finali restano nella pipeline.",
        }
        report["structured_sources"] = {row["kind"]: row for row in sections}
        report["football_data_research"] = (
            football_data_result.model_dump(mode="json")
            if football_data_result is not None
            else {"status": "DISABLED", "findings": [], "requested_fields": []}
        )
        if external is not None:
            from better.services.external_research import research_coverage

            report["external_research"] = {
                **external.model_dump(mode="json"),
                "coverage": research_coverage(external),
            }
            report["enriched_snapshot"] = enriched.model_dump(mode="json") if enriched is not None else None
        else:
            report["external_research"] = {
                "status": "DISABLED",
                "provider": None,
                "model": None,
                "searched_fields": [],
                "unresolved_fields": [],
                "findings": [],
                "cached": False,
            }
        return report
