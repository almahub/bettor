from pathlib import Path

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

metadata = MetaData()


def record_table(name):
    return Table(
        name,
        metadata,
        Column("id", String(128), primary_key=True),
        Column("entity_id", String(256), nullable=False, index=True),
        Column("match_id", String(256), nullable=True, index=True),
        Column("recorded_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSON, nullable=False),
    )


TABLE_NAMES = (
    "competitions",
    "teams",
    "players",
    "matches",
    "fixtures",
    "team_match_stats",
    "player_match_stats",
    "injuries",
    "lineups",
    "news_items",
    "odds_snapshots",
    "market_prices",
    "model_versions",
    "model_predictions",
    "agent_runs",
    "agent_evidence",
    "analysis_runs",
    "decisions",
    "journal_entries",
    "bet_results",
    "closing_prices",
    "validation_metrics",
    "shadow_decisions",
    "annotations",
    "identity_mappings",
    "identity_resolution_attempts",
    "historical_observations",
    "research_requests",
    "research_results",
    "research_findings",
    "research_sources",
    "pipeline_selections",
    "match_results",
    "settled_predictions",
    "validation_cohort",
    "canonical_predictions",
    "validation_observations",
    "validation_cost_events",
    "match_result_claims",
    "bet_candidates",
    "bet_slips",
    "bet_slip_selections",
    "bet_events",
    "bet_notes",
    "api_football_archive",
    "api_football_archive_checks",
    "api_football_archive_attempts",
)
TABLES = {name: record_table(name) for name in TABLE_NAMES}
IMMUTABLE_TABLES = tuple(TABLE_NAMES)


def make_engine(url: str = "sqlite:///data/better.db"):
    parsed = make_url(url)
    kwargs = {}
    if parsed.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if parsed.database in {None, "", ":memory:"}:
            kwargs["poolclass"] = StaticPool
        else:
            Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if parsed.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def configure(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")

    return engine


def install_immutability(connection):
    """Enforce immutability below the repository boundary on supported databases."""
    if connection.dialect.name == "sqlite":
        for name in IMMUTABLE_TABLES:
            for action in ("UPDATE", "DELETE"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{name}_{action.lower()} "
                    f"BEFORE {action} ON {name} BEGIN SELECT RAISE(ABORT, 'append-only record'); END"
                )
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION better_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'append-only record'; END; $$ LANGUAGE plpgsql"
        )
        for name in IMMUTABLE_TABLES:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS immutable_{name} ON {name}")
            connection.exec_driver_sql(
                f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
                "FOR EACH ROW EXECUTE FUNCTION better_immutable()"
            )


def initialize(engine):
    metadata.create_all(engine)
    with engine.begin() as connection:
        install_immutability(connection)
