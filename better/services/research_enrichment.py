"""Deterministic bridge from cited research findings to Better snapshots."""

from hashlib import sha256

from better.domain.enums import ReliabilityTier
from better.domain.models import LineupSnapshot, NewsItem, NewsSnapshot
from better.domain.research import (
    EnrichedField,
    EnrichedSnapshot,
    ResearchFindingStatus,
    ResearchResult,
    as_provenance,
    finding_is_resolved,
)
from better.storage.repository import canonical_hash

LINEUP_FIELDS = {"probable_lineups", "confirmed_lineups", "unavailable_players"}


def build_enriched_snapshot(
    match, result: ResearchResult | None, *, football_result=None, odds=None
) -> EnrichedSnapshot:
    fields = {}
    for finding in result.findings if result is not None else ():
        primary = min(
            finding.sources,
            key=lambda source: "ABCD".index(source.reliability_tier.value),
            default=None,
        )
        fields[finding.field] = EnrichedField(
            field=finding.field,
            value=finding.value,
            status=finding.status,
            confidence=finding.confidence,
            source_url=primary.url if primary else None,
            publisher=primary.publisher if primary else None,
            source_tier=primary.reliability_tier if primary else None,
            published_at=primary.published_at if primary else None,
            retrieved_at=primary.retrieved_at if primary else None,
            as_of=result.as_of,
            lineage=tuple(
                dict.fromkeys(
                    value
                    for source in finding.sources
                    for value in ((source.canonical_url or source.url), *source.derived_from)
                )
            ),
            conflicts=finding.conflicts,
            sources=finding.sources,
        )
    material = {name: field.model_dump(mode="json") for name, field in sorted(fields.items())}
    as_of = result.as_of if result is not None else football_result.as_of
    completed_at = max(value.completed_at for value in (result, football_result) if value is not None)
    return EnrichedSnapshot(
        snapshot_id="research:" + canonical_hash(material),
        match_id=match.match_id,
        as_of=as_of,
        captured_at=completed_at,
        fields=fields,
        structured_market_data=odds.model_dump(mode="json") if odds is not None else {},
        football_data_research=football_result.model_dump(mode="json")
        if football_result is not None
        else None,
        external_research=result.model_dump(mode="json") if result is not None else None,
        research_request_id=result.request_id if result is not None else None,
        provider=result.provider if result is not None else None,
        session_id=result.session_id if result is not None else None,
        response_id=result.response_id if result is not None else None,
    )


def _players(value, *names):
    if not isinstance(value, dict):
        return ()
    for name in names:
        raw = value.get(name)
        if isinstance(raw, (list, tuple)):
            return tuple(str(item.get("name") if isinstance(item, dict) else item) for item in raw if item)
    return ()


def _flatten_players(value):
    if isinstance(value, (list, tuple)):
        return tuple(str(item.get("name") if isinstance(item, dict) else item) for item in value if item)
    if isinstance(value, dict):
        return tuple(
            str(item.get("name") if isinstance(item, dict) else item)
            for raw in value.values()
            if isinstance(raw, (list, tuple))
            for item in raw
            if item
        )
    return ()


def normalized_qualitative_snapshots(match, enriched: EnrichedSnapshot):
    """Return only contract-valid legacy views consumed by current agents."""
    resolved = {name: field for name, field in enriched.fields.items() if finding_is_resolved(field.status)}
    news_items = []
    news_provenance = []
    for name, field in resolved.items():
        if name in {"probable_lineups", "confirmed_lineups"}:
            continue
        for source in field.sources:
            provenance = as_provenance(source, name)
            news_provenance.append(provenance)
            summary = field.value if isinstance(field.value, str) else f"{name}: {field.value}"
            news_items.append(
                NewsItem(
                    event_id=sha256(f"{match.match_id}:{name}:{summary}".encode()).hexdigest()[:24],
                    headline=str(summary),
                    category=name,
                    verified=field.status == ResearchFindingStatus.CONFIRMED
                    and source.reliability_tier == ReliabilityTier.A,
                    material=True,
                    provenance=provenance,
                )
            )
    snapshots = {}
    if news_items:
        snapshots["news"] = NewsSnapshot(
            snapshot_id=enriched.snapshot_id + ":news",
            match_id=match.match_id,
            items=tuple(news_items),
            captured_at=enriched.captured_at,
            provenance=tuple(dict.fromkeys(news_provenance)),
        )

    lineup = resolved.get("confirmed_lineups") or resolved.get("probable_lineups")
    unavailable = resolved.get("unavailable_players")
    if lineup or unavailable:
        sources = tuple(
            dict.fromkeys(
                source for field in (lineup, unavailable) if field is not None for source in field.sources
            )
        )
        provenance = tuple(as_provenance(source, "lineups") for source in sources)
        home = _players(lineup.value, "home_players", "home", "home_team") if lineup else ()
        away = _players(lineup.value, "away_players", "away", "away_team") if lineup else ()
        confirmed = bool(
            lineup
            and lineup.field == "confirmed_lineups"
            and lineup.status == ResearchFindingStatus.CONFIRMED
            and len(home) == len(away) == 11
        )
        probable = bool(lineup and home and away)
        snapshots["lineups"] = LineupSnapshot(
            snapshot_id=enriched.snapshot_id + ":lineups",
            match_id=match.match_id,
            status="CONFIRMED_LINEUP" if confirmed else "PROBABLE_LINEUP" if probable else "UNKNOWN",
            home_players=home,
            away_players=away,
            unavailable=_flatten_players(unavailable.value) if unavailable else (),
            sensitivity=0.5,
            captured_at=enriched.captured_at,
            provenance=provenance,
        )
    return snapshots
