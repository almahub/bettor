"""Read-only saved-agent configuration and recorded outcomes; no provider calls."""

from datetime import datetime, timezone

from better.observability.diagnostics import safe_message


def ai_agents_report(runtime):
    repository = runtime.repository
    sources = (
        ("external_research", "better-external-research-v1", "OPENAI_RESEARCH_AGENT_ID",
         "Notizie, indisponibilita, convocati, formazioni e contesto pre-match.",
         repository.all_records("research_results")),
        ("football_data_research", "better-football-data-research-v1", "OPENAI_FOOTBALL_DATA_AGENT_ID",
         "Statistiche e osservazioni quantitative mancanti, dopo la consultazione dell'archivio locale.",
         [row for row in repository.all_records("annotations") if row.get("requested_fields") is not None]),
    )
    agents = []
    for key, name, variable, role, records in sources:
        service = getattr(runtime, key, None)
        config = service.describe() if service else {"enabled": False, "ready": False}
        agent_id = config.get("agent_id")
        records = [r for r in records if agent_id and r.get("agent_id") == agent_id and r.get("request_id") and r.get("completed_at")]
        latest = max(records, key=lambda r: datetime.fromisoformat(r["completed_at"]), default=None)
        activity = None
        if latest:
            match = repository.get_match_record(latest["match_id"])
            findings = latest.get("findings") or []
            activity = {
                "completed_at": latest["completed_at"], "status": latest.get("status"),
                "match_id": latest["match_id"],
                "match": f"{match.home_team} - {match.away_team}" if match else latest["match_id"],
                "cached": bool(latest.get("cached")),
                "resolved_fields": sum(r.get("status") in {"FOUND", "CONFIRMED", "PROBABLE", "REPORTED"} for r in findings),
                "requested_fields": latest.get("searched_fields", latest.get("requested_fields", [])),
                "error": safe_message(latest["error"]) if latest.get("error") else None,
                "estimated_cost_usd": latest.get("estimated_cost"),
            }
        settings = getattr(service, "settings", None)
        budget = getattr(settings, "max_cost_per_match", None)
        agents.append({
            "key": key, "name": name, "env_variable": variable, "role": role,
            "enabled": bool(config.get("enabled")),
            "configured": bool(config.get("enabled") and config.get("ready") and agent_id),
            "agent_id": agent_id, "transport": config.get("transport"),
            "configuration_error": safe_message(config["configuration_error"]) if config.get("configuration_error") else None,
            "fields": config.get("fields", {}),
            "supported_competitions": config.get("supported_competitions", []),
            "budget_per_match_usd": str(budget) if budget is not None else None,
            "cache_ttl_seconds": getattr(settings, "recent_form_ttl_seconds", None),
            "recorded_runs": len(records), "latest": activity,
        })
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "verification": "LOCAL_RECORDS_ONLY", "agents": agents}
