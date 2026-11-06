import hashlib
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import insert, select

from better.domain.schemas import AnalysisRun
from better.storage.db import TABLES, initialize, make_engine


def canonical_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class Repository:
    def __init__(self, database_url: str = "sqlite:///data/better.db", *, engine=None):
        self.engine = engine if engine is not None else make_engine(database_url)
        initialize(self.engine)

    def close(self):
        self.engine.dispose()

    def save_match(self, match):
        document = match.model_dump(mode="json")
        with self.engine.begin() as connection:
            for table_name in ("matches", "fixtures"):
                self._append(
                    connection,
                    table_name,
                    identifier=canonical_hash(document),
                    entity_id=match.match_id,
                    match_id=match.match_id,
                    payload=document,
                )
        return match.match_id

    def save_identity_mapping(
        self, *, kind, canonical_id, target_id, confidence, method, verified_at, evidence
    ):
        if kind not in {"match", "team", "competition"} or not 0 <= confidence <= 1:
            raise ValueError("Invalid identity mapping")
        document = {
            "kind": kind,
            "source_system": "888it",
            "canonical_id": canonical_id,
            "target_system": "sportradar",
            "target_id": target_id,
            "status": "VERIFIED",
            "confidence": confidence,
            "method": method,
            "verified_at": verified_at.isoformat(),
            "evidence": evidence,
        }
        identifier = canonical_hash(document)
        with self.engine.begin() as connection:
            self._append(
                connection,
                "identity_mappings",
                identifier=identifier,
                entity_id=f"{kind}:{canonical_id}",
                match_id=canonical_id if kind == "match" else None,
                payload=document,
            )
        return identifier

    def identity_target(self, kind, canonical_id):
        rows = self.list_records("identity_mappings", last=1000, entity_id=f"{kind}:{canonical_id}")
        targets = {row["target_id"] for row in rows if row.get("status") == "VERIFIED"}
        if len(targets) > 1:
            raise ValueError(f"Conflicting verified identity mappings for {kind}:{canonical_id}")
        return next(iter(targets), None)

    def record_identity_attempt(self, *, match_id, attempted_at, result, reason, candidates, selected=None):
        document = {
            "match_id": match_id,
            "attempted_at": attempted_at.isoformat(),
            "result": result,
            "reason": reason,
            "selected": selected,
            "candidates": candidates,
        }
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "identity_resolution_attempts",
                identifier=canonical_hash(document),
                entity_id=match_id,
                match_id=match_id,
                payload=document,
            )

    def identity_coverage(self):
        attempts = TABLES["identity_resolution_attempts"]
        mappings = TABLES["identity_mappings"]
        with self.engine.connect() as connection:
            attempt_rows = list(connection.execute(select(attempts.c.payload)).scalars())
            mapping_rows = list(connection.execute(select(mappings.c.payload)).scalars())
        latest = {}
        for row in sorted(attempt_rows, key=lambda value: value["attempted_at"]):
            latest[row["match_id"]] = row
        reasons = {}
        for row in latest.values():
            if row["result"] == "RESOLVED":
                continue
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        resolved = sum(row["result"] == "RESOLVED" for row in latest.values())
        return {
            "attempted_matches": len(latest),
            "resolved_matches": resolved,
            "coverage": resolved / len(latest) if latest else 0.0,
            "failure_reasons": reasons,
            "verified_mappings": {
                kind: sum(row["kind"] == kind and row["status"] == "VERIFIED" for row in mapping_rows)
                for kind in ("match", "team", "competition")
            },
        }

    def save_result_identity_mapping(
        self,
        *,
        kind,
        source_id,
        provider,
        target_id,
        source_name,
        target_name,
        status,
        method,
        confidence,
        verified_at,
        source_url,
        normalization_version,
        provenance,
        identity,
    ):
        if kind not in {"match", "team", "competition"} or not 0 <= confidence <= 1:
            raise ValueError("Invalid result identity mapping")
        stable = {
            "scope": "result_identity",
            "kind": kind,
            "source_system": "888it",
            "source_id": source_id,
            "target_provider": provider,
            "target_id": target_id,
            "source_name": source_name,
            "target_name": target_name,
            "normalized_source_name": self._normalized_result_name(source_name),
            "normalized_target_name": self._normalized_result_name(target_name),
            "verification_status": status,
            "verification_method": method,
            "confidence": confidence,
            "source_url": source_url,
            "normalization_version": normalization_version,
            "provenance": provenance,
            "identity": identity,
        }
        existing = self.result_identity_mapping(kind, source_id, provider=provider)
        comparable = {key: existing.get(key) for key in stable} if existing else None
        if comparable and comparable.get("identity"):
            comparable["identity"] = {
                key: value for key, value in comparable["identity"].items() if key != "verified_at"
            }
        candidate_stable = dict(stable)
        if candidate_stable.get("identity"):
            candidate_stable["identity"] = {
                key: value for key, value in candidate_stable["identity"].items() if key != "verified_at"
            }
        if comparable == candidate_stable:
            return existing["mapping_id"]
        document = {
            **stable,
            "verified_at": verified_at.isoformat(),
            "version": 1
            + sum(
                row.get("scope") == "result_identity" and row.get("target_provider") == provider
                for row in self.list_records(
                    "identity_mappings", last=1000, entity_id=f"result:{kind}:{source_id}"
                )
            ),
            "supersedes": existing["mapping_id"] if existing else None,
        }
        identifier = "result-identity-mapping:" + canonical_hash(document)
        document["mapping_id"] = identifier
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "identity_mappings",
                identifier=identifier,
                entity_id=f"result:{kind}:{source_id}",
                match_id=source_id if kind == "match" else None,
                payload=document,
            )

    @staticmethod
    def _normalized_result_name(value):
        # Imported lazily to keep storage independent during module initialization.
        from better.providers.result_identity import normalize_team_name

        return normalize_team_name(value)

    def result_identity_mapping(self, kind, source_id, *, provider=None):
        rows = self.list_records("identity_mappings", last=1000, entity_id=f"result:{kind}:{source_id}")
        rows = [
            row
            for row in rows
            if row.get("scope") == "result_identity"
            and (provider is None or row.get("target_provider") == provider)
        ]
        if not rows:
            return None
        if provider is None:
            priorities = {"serie_c_official": 0, "api_football": 1, "football_data_org": 2}
            rows.sort(key=lambda row: (priorities.get(row["target_provider"], 99), -row["version"]))
            return rows[0]
        return max(rows, key=lambda row: row["version"])

    def save_result_claim(self, *, match_id, provider_match_id, result):
        stable = {
            "match_id": match_id,
            "provider": result.provider,
            "provider_match_id": provider_match_id,
            "home_score": result.home_score,
            "away_score": result.away_score,
            "regulation_score": [result.home_score, result.away_score],
            "extra_time_score": [result.extra_time_home, result.extra_time_away],
            "penalties": [result.penalties_home, result.penalties_away],
            "status": result.final_status,
            "source_tier": result.source_tier,
            "source_url": str(result.source_url),
            "published_at": result.published_at.isoformat() if result.published_at else None,
        }
        checksum = canonical_hash(stable)
        identifier = f"result-claim:{checksum}"
        if any(
            row.get("checksum") == checksum
            for row in self.list_records("match_result_claims", last=1000, entity_id=match_id)
        ):
            return identifier
        document = {
            **stable,
            "retrieved_at": result.retrieved_at.isoformat(),
            "verified_at": result.verified_at.isoformat(),
            "checksum": checksum,
        }
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "match_result_claims",
                identifier=identifier,
                entity_id=match_id,
                match_id=match_id,
                payload=document,
            )

    def historical_collection_status(self):
        rows = self.list_records("historical_observations", last=1000)
        matches = {row["match"]["match_id"] for row in rows}
        return {
            "observations": len(rows),
            "matches": len(matches),
            "by_purpose": {
                purpose: sum(row["purpose"] == purpose for row in rows)
                for purpose in sorted({row["purpose"] for row in rows})
            },
            "eligible_for_model_validation": sum(
                row.get("eligible_for_model_validation") is True for row in rows
            ),
            "anti_leakage": "ENFORCED",
            "note": (
                "Le osservazioni sono point-in-time e non entrano nella validazione finche una "
                "predizione pre-match e un esito successivo non vengono collegati separatamente."
            ),
        }

    def select_for_collection(self, *, match, selected_at):
        if selected_at.tzinfo is None or match.kickoff.tzinfo is None or selected_at >= match.kickoff:
            raise ValueError("Pipeline selection must be timezone-aware and pre-match")
        document = {
            "match": match.model_dump(mode="json"),
            "selected_at": selected_at.isoformat(),
            "expires_at": match.kickoff.isoformat(),
            "status": "SELECTED",
        }
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "pipeline_selections",
                identifier=canonical_hash(document),
                entity_id=match.match_id,
                match_id=match.match_id,
                payload=document,
            )

    def active_collection_selections(self, as_of):
        if as_of.tzinfo is None:
            raise ValueError("Selection cutoff must be timezone-aware")
        rows = self.list_records("pipeline_selections", last=1000)
        latest = {}
        for row in sorted(rows, key=lambda item: item["selected_at"]):
            latest[row["match"]["match_id"]] = row
        return [
            row
            for row in latest.values()
            if row.get("status") == "SELECTED" and datetime.fromisoformat(row["expires_at"]) > as_of
        ]

    def has_historical_checkpoint(self, match_id, checkpoint):
        return any(
            row.get("checkpoint") == checkpoint
            for row in self.all_records("historical_observations", entity_id=match_id)
        )

    def get_match_record(self, match_id):
        rows = self.list_records("matches", last=1000, entity_id=match_id)
        if not rows:
            return None
        from better.domain.models import Match

        return Match.model_validate(rows[0])

    def analysis_runs_for_match(self, match_id):
        table = TABLES["analysis_runs"]
        with self.engine.connect() as connection:
            payloads = connection.execute(
                select(table.c.payload)
                .where(table.c.match_id == str(match_id))
                .order_by(table.c.recorded_at.asc(), table.c.id.asc())
            ).scalars()
            return [AnalysisRun.model_validate(payload) for payload in payloads]

    def settled_validation_history(self, as_of):
        from better.domain.models import HistoricalPrediction

        return tuple(
            HistoricalPrediction.model_validate(row)
            for row in reversed(self.list_records("settled_predictions", last=1000))
            if datetime.fromisoformat(row["settled_at"]) <= as_of
        )

    def archive_pre_match_observation(
        self, *, match, as_of, collected_at, snapshots, failures, purpose, checkpoint=None
    ):
        if any(value.tzinfo is None for value in (as_of, collected_at, match.kickoff)):
            raise ValueError("Historical timestamps must be timezone-aware")
        if as_of > collected_at or collected_at >= match.kickoff:
            raise ValueError("Historical observation violates the pre-match cutoff")
        document = {
            "schema_version": "1.0",
            "purpose": purpose,
            "checkpoint": checkpoint,
            "match": match.model_dump(mode="json"),
            "as_of": as_of.isoformat(),
            "collected_at": collected_at.isoformat(),
            "feature_cutoff": collected_at.isoformat(),
            "snapshots": {key: value.model_dump(mode="json") for key, value in sorted(snapshots.items())},
            "failures": {
                key: value.detail.model_dump(mode="json") for key, value in sorted(failures.items())
            },
            "eligible_for_model_validation": False,
            "validation_note": (
                "Raw point-in-time observation only. It becomes validation evidence only after a "
                "prediction made at or before this cutoff and a later independently recorded outcome."
            ),
        }
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "historical_observations",
                identifier=canonical_hash(document),
                entity_id=match.match_id,
                match_id=match.match_id,
                payload=document,
            )

    def _append(self, connection, table_name, *, identifier, entity_id, match_id, payload):
        table = TABLES[table_name]
        old = connection.execute(select(table.c.payload).where(table.c.id == identifier)).scalar_one_or_none()
        if old is not None:
            if canonical_hash(old) != canonical_hash(payload):
                raise ValueError(f"Cannot overwrite immutable {table_name} record {identifier}")
            return identifier
        connection.execute(
            insert(table).values(
                id=identifier,
                entity_id=str(entity_id),
                match_id=match_id,
                recorded_at=datetime.now(timezone.utc),
                payload=payload,
            )
        )
        return identifier

    def append_pre_match(self, *, analysis_id, match_id, as_of, kickoff, payload) -> str:
        if as_of.tzinfo is None or kickoff.tzinfo is None or as_of >= kickoff:
            raise ValueError("Pre-match journal requires aware timestamp strictly before kickoff")
        identifier = str(uuid5(NAMESPACE_URL, f"better:journal:{analysis_id}:pre_match"))
        document = {
            "phase": "PRE_MATCH",
            "analysis_id": str(analysis_id),
            "as_of": as_of.isoformat(),
            "kickoff": kickoff.isoformat(),
            "snapshot": payload,
        }
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "journal_entries",
                identifier=identifier,
                entity_id=analysis_id,
                match_id=match_id,
                payload=document,
            )

    def save_run(self, run: AnalysisRun, *, shadow_records=()):
        payload = run.model_dump(mode="json")
        match_id, analysis_id = run.match_id, str(run.analysis_id)
        with self.engine.begin() as connection:
            self._append(
                connection,
                "analysis_runs",
                identifier=analysis_id,
                entity_id=analysis_id,
                match_id=match_id,
                payload=payload,
            )
            self._append(
                connection,
                "decisions",
                identifier=analysis_id,
                entity_id=analysis_id,
                match_id=match_id,
                payload=run.decision.model_dump(mode="json"),
            )
            match = run.snapshot.match
            for table_name, entity_id, document in (
                ("matches", match_id, match.model_dump(mode="json")),
                ("fixtures", match_id, match.model_dump(mode="json")),
                ("competitions", match.competition, {"name": match.competition}),
                ("teams", match.home_team_id, {"id": match.home_team_id, "name": match.home_team}),
                ("teams", match.away_team_id, {"id": match.away_team_id, "name": match.away_team}),
            ):
                self._append(
                    connection,
                    table_name,
                    identifier=canonical_hash(document),
                    entity_id=entity_id,
                    match_id=match_id,
                    payload=document,
                )
            for name, snapshot in (
                ("team_match_stats", run.snapshot.stats),
                ("odds_snapshots", run.snapshot.odds),
                ("lineups", run.snapshot.lineups),
                ("news_items", run.snapshot.news),
            ):
                if snapshot:
                    document = snapshot.model_dump(mode="json")
                    self._append(
                        connection,
                        name,
                        identifier=canonical_hash(document),
                        entity_id=snapshot.snapshot_id,
                        match_id=match_id,
                        payload=document,
                    )
            if run.snapshot.odds:
                for quote in run.snapshot.odds.quotes:
                    document = quote.model_dump(mode="json")
                    self._append(
                        connection,
                        "market_prices",
                        identifier=canonical_hash(document),
                        entity_id=f"{quote.bookmaker}:{quote.market.type}:{quote.market.selection}",
                        match_id=match_id,
                        payload=document,
                    )
            for agent_id, result in run.results.items():
                self._append(
                    connection,
                    "agent_runs",
                    identifier=f"{analysis_id}:{agent_id}",
                    entity_id=analysis_id,
                    match_id=match_id,
                    payload=result.model_dump(mode="json"),
                )
                for evidence in result.evidence:
                    self._append(
                        connection,
                        "agent_evidence",
                        identifier=str(evidence.evidence_id),
                        entity_id=analysis_id,
                        match_id=match_id,
                        payload=evidence.model_dump(mode="json"),
                    )
                if result.payload and result.payload.kind == "predictive":
                    for prediction in result.payload.model_predictions:
                        self._append(
                            connection,
                            "model_predictions",
                            identifier=str(prediction.prediction_id),
                            entity_id=prediction.model,
                            match_id=match_id,
                            payload=prediction.model_dump(mode="json"),
                        )
                        version = {
                            "model": prediction.model,
                            "version": prediction.model_version,
                            "feature_version": prediction.feature_version,
                        }
                        self._append(
                            connection,
                            "model_versions",
                            identifier=canonical_hash(version),
                            entity_id=prediction.model,
                            match_id=None,
                            payload=version,
                        )
                if result.payload and result.payload.kind == "validation":
                    for validation in result.payload.validations:
                        document = validation.model_dump(mode="json")
                        self._append(
                            connection,
                            "validation_metrics",
                            identifier=f"{analysis_id}:{canonical_hash(document)}",
                            entity_id=validation.model_id,
                            match_id=match_id,
                            payload=document,
                        )
            for record in shadow_records:
                document = record.model_dump(mode="json")
                self._append(
                    connection,
                    "shadow_decisions",
                    identifier=f"{analysis_id}:{record.stage}",
                    entity_id=analysis_id,
                    match_id=match_id,
                    payload=document,
                )

    def get_run(self, analysis_id) -> AnalysisRun | None:
        table = TABLES["analysis_runs"]
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(table.c.payload).where(table.c.id == str(analysis_id))
            ).scalar_one_or_none()
        return AnalysisRun.model_validate(payload) if payload is not None else None

    def list_visible_runs(self, *, last=50, offset=0):
        runs, annotations = TABLES["analysis_runs"], TABLES["annotations"]
        hidden = select(annotations.c.id).where(annotations.c.id == "journal-hidden:" + runs.c.id).exists()
        query = (
            select(runs.c.payload)
            .where(~hidden)
            .order_by(runs.c.recorded_at.desc(), runs.c.id.desc())
            .limit(last)
            .offset(offset)
        )
        with self.engine.connect() as connection:
            return list(connection.execute(query).scalars())

    def hide_runs(self, analysis_ids):
        """Remove from the dashboard list, preserving immutable audit and bank data."""
        identifiers = sorted({str(identifier) for identifier in analysis_ids})
        runs = TABLES["analysis_runs"]
        with self.engine.begin() as connection:
            found = dict(
                connection.execute(select(runs.c.id, runs.c.match_id).where(runs.c.id.in_(identifiers))).all()
            )
            if len(found) != len(identifiers):
                raise KeyError("Analysis not found")
            for identifier in identifiers:
                self._append(
                    connection,
                    "annotations",
                    identifier=f"journal-hidden:{identifier}",
                    entity_id=identifier,
                    match_id=found[identifier],
                    payload={"override_type": "JOURNAL_HIDDEN", "analysis_id": identifier},
                )
        return identifiers

    def all_records(self, table_name, *, entity_id=None, match_id=None):
        """Complete internal ledger read; UI limits must not truncate operational state."""
        table = TABLES[table_name]
        query = select(table.c.payload).order_by(table.c.recorded_at.desc(), table.c.id.desc())
        if entity_id is not None:
            query = query.where(table.c.entity_id == str(entity_id))
        if match_id is not None:
            query = query.where(table.c.match_id == str(match_id))
        with self.engine.connect() as connection:
            return list(connection.execute(query).scalars())

    def list_records(self, table_name, *, last=20, entity_id=None):
        if not 1 <= last <= 1000:
            raise ValueError("last must be between 1 and 1000")
        table = TABLES[table_name]
        query = select(table.c.payload).order_by(table.c.recorded_at.desc(), table.c.id.desc()).limit(last)
        if entity_id is not None:
            query = query.where(table.c.entity_id == str(entity_id))
        with self.engine.connect() as connection:
            return list(connection.execute(query).scalars())

    def annotate(self, analysis_id, *, author, reason, created_at):
        run = self.get_run(analysis_id)
        if run is None:
            raise KeyError("analysis not found")
        if not author.strip() or not reason.strip() or created_at.tzinfo is None:
            raise ValueError("Annotation requires author, reason and aware timestamp")
        document = {
            "override_type": "ANNOTATION",
            "author": author,
            "reason": reason,
            "created_at": created_at.isoformat(),
            "analysis_id": str(analysis_id),
        }
        with self.engine.begin() as connection:
            return self._append(
                connection,
                "annotations",
                identifier=canonical_hash(document),
                entity_id=str(analysis_id),
                match_id=run.match_id,
                payload=document,
            )

    def settle(
        self,
        analysis_id,
        *,
        outcome: int,
        closing_odds: float | None,
        settled_at,
        review_tag="UNEXPLAINED_VARIANCE",
    ):
        from better.system_zero.validation import closing_line_value

        run = self.get_run(analysis_id)
        if run is None:
            raise KeyError("analysis not found")
        if outcome not in {0, 1} or settled_at.tzinfo is None or settled_at < run.snapshot.match.kickoff:
            raise ValueError("Settlement requires binary outcome after kickoff")
        decision = run.decision
        from decimal import Decimal

        profit = (
            decision.stake.amount * Decimal(str(decision.current_odds - 1))
            if outcome and decision.current_odds
            else -decision.stake.amount
        )
        document = {
            "phase": "POST_MATCH",
            "analysis_id": str(analysis_id),
            "outcome": outcome,
            "settled_at": settled_at.isoformat(),
            "closing_odds": closing_odds,
            "theoretical_profit": str(profit),
            "review_tag": review_tag,
            "clv": closing_line_value(decision.current_odds, closing_odds)
            if closing_odds and decision.current_odds
            else None,
        }
        with self.engine.begin() as connection:
            self._append(
                connection,
                "bet_results",
                identifier=str(analysis_id),
                entity_id=str(analysis_id),
                match_id=run.match_id,
                payload=document,
            )
            self._append(
                connection,
                "journal_entries",
                identifier=f"{analysis_id}:post_match",
                entity_id=str(analysis_id),
                match_id=run.match_id,
                payload=document,
            )
            if closing_odds is not None:
                self._append(
                    connection,
                    "closing_prices",
                    identifier=str(analysis_id),
                    entity_id=str(analysis_id),
                    match_id=run.match_id,
                    payload={
                        "analysis_id": str(analysis_id),
                        "closing_odds": closing_odds,
                        "settled_at": settled_at.isoformat(),
                    },
                )
        return document
