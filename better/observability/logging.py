import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record):
        fields = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        for key in (
            "analysis_id",
            "run_id",
            "match_id",
            "agent",
            "duration_ms",
            "status",
            "retry_count",
            "source_kind",
            "provider",
            "degradations",
        ):
            if hasattr(record, key):
                fields[key] = getattr(record, key)
        return json.dumps(fields, default=str)


class ConsoleFormatter(logging.Formatter):
    GROUPS = {
        "football_data_analyst": "DATA",
        "football_odds_analyst": "ODDS",
        "lineup_player_impact": "LINEUP",
        "news_intelligence": "NEWS",
        "model_validator": "VALIDATE",
        "predictive_model": "MODEL",
        "tactical_analyst": "TACTICAL",
        "market_selection": "MARKET",
        "value_bettor": "VALUE",
        "risk_manager": "RISK",
        "devils_advocate": "CONTRA",
        "professional_bettor": "FINAL",
        "betting_journal": "JOURNAL",
    }

    def format(self, record):
        event = record.getMessage()
        if event == "orchestrator_agent_result_received":
            group = self.GROUPS.get(record.agent, "AGENT")
            message = f"[{group:<8}] {record.agent:<26} {record.status:<18} {record.duration_ms:9.2f} ms"
            limitations = getattr(record, "degradations", ())
            if limitations:
                message += "\n" + "\n".join(
                    f"           {d['impact']}: {d['code']} | missing={','.join(d['missing_checks'])}"
                    for d in limitations
                )
            return message
        if event == "source_collection_completed":
            return (
                f"[SOURCE  ] {record.source_kind:<26} {record.status:<18} {record.duration_ms:9.2f} ms"
                f"  provider={record.provider}"
            )
        return f"[{record.levelname}] {event}"


def configure_logging(*, console_format="json", log_file=None, verbose=False):
    """Console is presentation; the optional rotating file always retains JSON."""
    handler = logging.StreamHandler()
    handler.setFormatter(ConsoleFormatter() if console_format == "console" else JsonFormatter())
    handlers = [handler]
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)
    logger = logging.getLogger("better")
    for previous in logger.handlers:
        previous.close()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers = handlers
    logger.propagate = False
