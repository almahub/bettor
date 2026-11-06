import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from better.config import Settings
from better.domain.schemas import AnalysisRequest
from better.providers.fake import DEMO_CASES
from better.services.application import make_application
from better.services.report_service import human_report


def parser():
    cli = argparse.ArgumentParser(prog="better", description="Football evidence-first decision support")
    cli.add_argument("--config", default="config", help="Configuration directory")
    cli.add_argument("--database", help="SQLAlchemy database URL")
    cli.add_argument("--log-format", choices=("console", "json"), default="console")
    cli.add_argument("--log-file", type=Path, default=Path("data/logs/better.jsonl"))
    cli.add_argument("--verbose", action="store_true", help="Include internal DEBUG events")
    commands = cli.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--match", required=True)
    analyze.add_argument("--market", action="append")
    analyze.add_argument("--demo", choices=DEMO_CASES, help="Explicit synthetic offline scenario")
    analyze.add_argument("--as-of")
    analyze.add_argument(
        "--mode", choices=("EARLY", "PRE_MATCH", "PRE_LINEUP", "POST_LINEUP"), default="PRE_MATCH"
    )
    analyze.add_argument("--depth", choices=("FAST", "FULL"), default="FULL")
    analyze.add_argument("--parent-analysis-id")
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--output", type=Path)
    commands.add_parser("health")
    commands.add_parser("identity-coverage", help="Show persisted 888 to Sportradar coverage")
    commands.add_parser("history-status", help="Show point-in-time historical collection status")
    commands.add_parser("pipeline-status", help="Show central collection, validation and storage status")
    sources = commands.add_parser("sources-check", help="Check API-Football account and season access")
    sources.add_argument("--league", type=int, default=5)
    sources.add_argument("--season", type=int, default=datetime.now(timezone.utc).year)
    archive = commands.add_parser(
        "fetch-history", help="Archive native API-Football fixtures; not a backtest"
    )
    archive.add_argument("--league", type=int, required=True)
    archive.add_argument("--season", type=int, required=True)
    archive.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate-models")
    validate.add_argument("--demo", choices=DEMO_CASES)
    journal = commands.add_parser("journal")
    journal.add_argument("--last", type=int, default=20)
    serve = commands.add_parser("serve")
    serve.add_argument("--demo", choices=DEMO_CASES)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    replay = commands.add_parser("replay")
    replay.add_argument("--analysis-id", required=True)
    pipeline_replay = commands.add_parser("replay-pipeline")
    pipeline_replay.add_argument("--analysis-id", required=True)
    return cli


async def execute(args):
    if args.command in {"sources-check", "fetch-history"}:
        from better.providers.api_football import APIFootballProvider
        from better.services.provider_factory import SourceConfiguration

        if args.league <= 0 or not 2000 <= args.season <= 2100:
            raise ValueError("Invalid league or season")
        source_config = SourceConfiguration.load(Settings.load(args.config).sources_config)
        provider = APIFootballProvider(source_config.api_football)
        try:
            if args.command == "sources-check":
                return json.dumps(await provider.check_access(args.league, args.season), indent=2)
            response = await provider.request("fixtures", {"league": args.league, "season": args.season})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(response, default=str, indent=2), encoding="utf-8")
            return json.dumps(
                {
                    "fixtures": len(response["data"]),
                    "output": str(args.output),
                    "note": "Native archive only; no point-in-time validation implied",
                }
            )
        finally:
            await provider.close()
    settings = Settings.load(args.config)
    if args.database:
        settings = Settings.model_validate({**settings.model_dump(), "database_url": args.database})
    requested_as_of = datetime.fromisoformat(args.as_of) if getattr(args, "as_of", None) else None
    application_as_of = requested_as_of or datetime.now(timezone.utc)
    app = make_application(
        settings=settings,
        demo=getattr(args, "demo", None),
        as_of=application_as_of,
    )
    try:
        if args.command == "analyze":
            fields = {
                "match_id": args.match,
                # None means a live analysis. Supplying the CLI invocation timestamp
                # would incorrectly activate historical point-in-time restrictions.
                "as_of": requested_as_of,
                "mode": args.mode,
                "depth": args.depth,
                "parent_analysis_id": args.parent_analysis_id,
            }
            if args.market:
                fields["markets"] = args.market
            run = await app.analyze(AnalysisRequest.model_validate(fields))
            output = run.model_dump_json(indent=2) if args.json else human_report(run)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output, encoding="utf-8")
            return output
        if args.command == "health":
            return json.dumps(
                {"status": "ok", "execution_enabled": False, "system_one": app.shadow.health()}, indent=2
            )
        if args.command == "identity-coverage":
            return json.dumps(app.repository.identity_coverage(), indent=2)
        if args.command == "history-status":
            return json.dumps(app.repository.historical_collection_status(), indent=2)
        if args.command == "pipeline-status":
            from better.services.data_pipeline import DataPipelineService

            return json.dumps(DataPipelineService(app.repository).report(), indent=2)
        if args.command == "journal":
            return json.dumps(app.repository.list_records("journal_entries", last=args.last), indent=2)
        if args.command == "validate-models":
            if args.demo:
                run = await app.analyze(AnalysisRequest(match_id="demo-match", as_of=application_as_of))
                return run.results["model_validator"].model_dump_json(indent=2)
            return json.dumps(app.repository.list_records("validation_metrics", last=1000), indent=2)
        if args.command == "replay":
            from better.system_one import ShadowRecord
            from better.system_one.replay import replay_records

            if not app.shadow.settings.enabled or app.shadow.provider is None:
                raise ValueError("Replay requires SYSTEM_ONE_ENABLED=true with a configured shadow provider")
            records = [
                ShadowRecord.model_validate(record)
                for record in app.repository.list_records("shadow_decisions", entity_id=args.analysis_id)
            ]
            if not records:
                raise ValueError("No stored shadow records found for this analysis")
            comparisons = await replay_records(records, app.shadow.provider, app.shadow.settings)
            return json.dumps([comparison.model_dump(mode="json") for comparison in comparisons], indent=2)
        if args.command == "replay-pipeline":
            from better.services.replay_service import replay_analysis

            original = app.repository.get_run(args.analysis_id)
            if original is None:
                raise ValueError("Analysis not found")
            parent = (
                app.repository.get_run(original.parent_analysis_id) if original.parent_analysis_id else None
            )
            replayed = await replay_analysis(original, parent_runs=(parent,) if parent else ())
            return json.dumps(
                {
                    "analysis_id": str(replayed.analysis_id),
                    "decision": replayed.decision.status.value,
                    "decision_matches": replayed.decision == original.decision,
                    "agent_outputs_match": replayed.results == original.results,
                    "offline": True,
                },
                indent=2,
            )
    finally:
        await app.close()


def main(argv=None):
    from better.observability.logging import configure_logging

    cli = parser()
    args = cli.parse_args(argv)
    configure_logging(console_format=args.log_format, log_file=args.log_file, verbose=args.verbose)
    if args.command == "serve":
        import uvicorn

        from better.api.app import create_app

        try:
            settings = Settings.load(args.config)
            if args.database:
                settings = Settings.model_validate({**settings.model_dump(), "database_url": args.database})
            runtime = make_application(settings=settings, demo=args.demo)
        except (ValueError, KeyError) as error:
            cli.error(str(error))
        try:
            uvicorn.run(create_app(runtime), host=args.host, port=args.port)
        finally:
            asyncio.run(runtime.close())
        return
    try:
        print(asyncio.run(execute(args)))
    except (ValueError, KeyError) as error:
        cli.error(str(error))
    except Exception as error:
        from better.domain.errors import BetterError

        if isinstance(error, BetterError):
            cli.error(f"{error.detail.code.value}: {error}")
        raise


if __name__ == "__main__":
    main()
