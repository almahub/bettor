from better.domain.schemas import AnalysisRun


def human_report(run: AnalysisRun) -> str:
    """One source of truth: all display values come from the persisted analysis JSON."""
    match, decision = run.snapshot.match, run.decision
    lines = [
        f"MATCH: {match.home_team} vs {match.away_team}",
        f"COMPETITION: {match.competition}",
        f"AS OF: {run.as_of.isoformat()}",
        f"MODE: {run.mode.value} / {run.depth.value}",
        f"DATA QUALITY: {decision.data_quality.score:.1%}",
        f"FINAL STATUS: {decision.status.value}",
    ]
    if "synthetic" in match.home_team.lower() or "synthetic" in match.competition.lower():
        lines.insert(0, "SYNTHETIC DEMONSTRATION - NO REAL MATCH DATA")
    if decision.market:
        lines.append(f"MARKET: {decision.market.type.value} / {decision.market.selection}")
    for label, value, fmt in (
        ("CURRENT ODDS", decision.current_odds, ".3f"),
        ("MINIMUM ACCEPTABLE ODDS", decision.minimum_acceptable_odds, ".3f"),
        ("FAIR ODDS", decision.fair_odds, ".3f"),
        ("MODEL PROBABILITY", decision.estimated_probability, ".2%"),
        ("EDGE (probability points)", decision.estimated_edge, "+.2%"),
        ("EV per unit", decision.expected_value, "+.4f"),
    ):
        lines.append(f"{label}: {format(value, fmt) if value is not None else 'unavailable'}")
    lines.extend(
        [
            f"CONFIDENCE: {decision.confidence:.1%} (operational indicator)",
            f"RISK: {decision.risk_level} (operational sizing/gates, not probability of loss)",
            f"THEORETICAL STAKE: {decision.stake.amount} ({decision.stake.bankroll_fraction:.2%} bankroll)",
        ]
    )
    risk = run.results.get("risk_manager")
    candidate = getattr(risk.payload, "candidate", None) if risk else None
    if candidate:
        minimum_ev = run.input_manifest.get("resolved_config", {}).get("minimum_ev")
        if minimum_ev is not None:
            lines.append(
                f"MINIMUM ODDS BASIS: (1 + minimum EV {minimum_ev:.2%}) / "
                f"conservative probability {candidate.conservative_probability:.4%}"
            )
    lines.append("CONFIDENCE COMPONENTS (minimum, not agent voting or win probability):")
    lines.extend(
        f"  {name}: {value:.4%}" + (" <- weakest" if value == decision.confidence else "")
        for name, value in decision.confidence_components.items()
    )
    lines.append("AGENT LIMITATIONS:")
    assessed = {(d.agent_id, d.code): d for d in decision.degradations}
    for agent_id, result in sorted(run.results.items()):
        if result.status.value == "PARTIAL" and not result.degradations:
            lines.append(f"  {agent_id}: UNCLASSIFIED legacy PARTIAL; impact not recorded")
        for limitation in result.degradations:
            applied = assessed.get((agent_id, limitation.code))
            scope = (
                "applies to decision"
                if applied and applied.applies_to_decision
                else "other market"
                if applied
                else "post-decision agent"
            )
            effect = (
                f"confidence cap {limitation.confidence_cap:.1%}"
                if limitation.confidence_cap is not None
                else "blocks BET"
                if limitation.critical
                else "no additional confidence penalty"
            )
            lines.extend(
                [
                    f"  {agent_id}: {result.status.value} / {limitation.impact} / {limitation.code}",
                    f"    Missing checks: {', '.join(limitation.missing_checks)}",
                    f"    Gates: {', '.join(limitation.affected_gates)}; {scope}; {effect}",
                    f"    Reason: {limitation.reason}",
                ]
            )
    lines.append("GATES:")
    lines.extend(f"  {trace.gate}: {trace.status} - {trace.reason}" for trace in decision.decision_trace)
    if decision.watch_conditions:
        lines.append("WATCH CONDITIONS:")
        lines.extend(
            f"  {condition.type} {condition.operator} {condition.target}"
            for condition in decision.watch_conditions
        )
    lines.append("INVALIDATION CONDITIONS:")
    lines.extend(f"  {condition}" for condition in decision.invalidation_conditions)
    lines.append(f"ANALYSIS ID: {run.analysis_id}")
    return "\n".join(lines)
