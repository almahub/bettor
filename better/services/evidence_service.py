from uuid import UUID

from better.domain.models import Evidence


def evidence_roots(
    evidence: tuple[Evidence, ...], external_ids: set[UUID] | None = None
) -> dict[UUID, frozenset[str]]:
    """Resolve shared ancestry; multiple derivatives are never independent confirmations."""
    by_id = {e.evidence_id: e for e in evidence}
    if len(by_id) != len(evidence):
        raise ValueError("duplicate evidence identifiers")
    external_ids = external_ids or set()
    resolved = {}

    def visit(identifier, path):
        if identifier in path:
            raise ValueError("evidence dependency cycle")
        if identifier in resolved:
            return resolved[identifier]
        if identifier not in by_id:
            if identifier in external_ids:
                return frozenset({f"prediction:{identifier}"})
            raise ValueError("evidence references unavailable parent")
        item = by_id[identifier]
        origins = {f"{p.primary_source_id or p.source_id}:{p.raw_reference}" for p in item.provenance}
        for parent in item.derived_from:
            origins.update(visit(parent, path | {identifier}))
        resolved[identifier] = frozenset(origins)
        return resolved[identifier]

    for identifier in by_id:
        visit(identifier, set())
    return resolved
