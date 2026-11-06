"""Read-only diagnostics of settled predictions; never changes Validator samples."""

import math
from collections import defaultdict
from datetime import datetime, timezone


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarize(rows):
    size = len(rows)
    if not size:
        return {
            "count": 0,
            "accuracy": None,
            "mean_probability": None,
            "brier_score": None,
            "calibration_gap": None,
        }
    predicted = sum(r["probability"] for r in rows) / size
    observed = sum(r["correct"] for r in rows) / size
    return {
        "count": size,
        "accuracy": observed,
        "mean_probability": predicted,
        "brier_score": sum(r["brier"] for r in rows) / size,
        "calibration_gap": observed - predicted,
    }


class ModelDiagnostics:
    def __init__(self, repository):
        self.repository = repository

    def report(self):
        groups = defaultdict(list)
        seen = set()
        excluded = duplicates = 0
        observations = sorted(
            self.repository.all_records("validation_observations"),
            key=lambda r: (r.get("created_at", ""), r.get("prediction_id", "")),
        )
        for item in observations:
            try:
                source = item.get("source_type", "UNKNOWN")
                key = (source, item["match_id"], item["model_name"], item["model_version"], item["market"])
                if key in seen:
                    duplicates += 1
                    continue
                if item.get("inclusion_status") != "VALIDATION_INCLUDED":
                    raise ValueError()
                if not (
                    parse_time(item["data_cutoff"]) <= parse_time(item["as_of"]) < parse_time(item["kickoff"])
                    and (source == "HISTORICAL_BACKTEST" or parse_time(item["created_at"]) < parse_time(item["kickoff"]))
                    and parse_time(item["kickoff"]) <= parse_time(item["settled_at"]) <= datetime.now(timezone.utc)
                ):
                    raise ValueError()
                probabilities = item["probabilities"]
                if not probabilities or item["actual_selection"] not in probabilities:
                    raise ValueError()
                if (
                    any(
                        not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1
                        for p in probabilities.values()
                    )
                    or abs(sum(probabilities.values()) - 1) > 1e-6
                ):
                    raise ValueError()
                top = max(sorted(probabilities), key=probabilities.get)
                probability = probabilities[top]
                brier = sum(
                    (p - int(selection == item["actual_selection"])) ** 2
                    for selection, p in probabilities.items()
                ) / len(probabilities)
                value = item.get("data_quality")
                if isinstance(value, dict):
                    value = value.get("score")
                basis = "Qualità dati"
                if value is None:
                    value = item.get("input_completeness")
                    basis = "Completezza input"
                if value is None:
                    quality = "Non registrata"
                else:
                    value = float(value)
                    if not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError()
                    bucket = min(3, int(value * 4))
                    quality = f"{basis}: {bucket * 25}–{(bucket + 1) * 25}%"
                bucket = min(9, int(round(probability * 100, 8) // 10))
                row = {
                    "probability": probability,
                    "correct": int(top == item["actual_selection"]),
                    "brier": brier,
                    "market": item["market"],
                    "version": item["model_name"] + " " + item["model_version"],
                    "quality": quality,
                    "probability_band": f"{bucket * 10:02d}–{(bucket + 1) * 10}%",
                }
                groups[source].append(row)
                seen.add(key)
            except (ValueError, TypeError, KeyError, AttributeError):
                excluded += 1
        result = {}
        for source, rows in sorted(groups.items()):
            dimensions = {}
            for field in ("market", "probability_band", "quality", "version"):
                buckets = defaultdict(list)
                for row in rows:
                    buckets[row[field]].append(row)
                dimensions[field] = [
                    {"label": label, **summarize(values)} for label, values in sorted(buckets.items())
                ]
            result[source] = {"summary": summarize(rows), "dimensions": dimensions}
        return {
            "sources": result,
            "excluded": excluded,
            "duplicates_ignored": duplicates,
            "note": "Una previsione per partita/modello/versione/mercato, indipendentemente dalle multiple. LIVE_FORWARD e storico separati; il campione operativo non viene modificato. Calibrazione e accuratezza riguardano l’esito più probabile. Brier medio sulle classi: confrontare lo stesso mercato; più basso è meglio. Lo storico di risultati senza previsioni non entra nelle metriche. Campioni piccoli non dimostrano miglioramenti.",
        }
