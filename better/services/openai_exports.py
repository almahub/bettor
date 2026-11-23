"""Validated, deduplicated usage exports; one atomic pointer commits both files."""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from better.services.credential_store import atomic_write


def validate_export(payload, kind):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or len(payload["data"]) > 2000:
        raise ValueError("Export OpenAI non valido")
    windows = []
    for b in payload["data"]:
        if not isinstance(b, dict):
            raise ValueError("Bucket export non valido")
        start = b.get("start_time")
        end = b.get("end_time", start + 3600 if type(start) is int else 0)
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or not isinstance(b.get("results"), list):
            raise ValueError("Periodo export non valido")
        for r in b["results"]:
            if not isinstance(r, dict):
                raise ValueError("Riga export non valida")
            if kind == "costs":
                amount = r.get("amount") or {}
                if not isinstance(amount, dict) or not {"value", "currency"} <= amount.keys():
                    raise ValueError("Importo export non valido")
                try:
                    value = Decimal(str(amount.get("value", "0")))
                except InvalidOperation:
                    raise ValueError("Importo export non valido") from None
                if not value.is_finite() or value < 0 or str(amount.get("currency", "usd")).upper() not in {"USD", "EUR"}:
                    raise ValueError("Importo export non valido")
            else:
                for k in ("num_model_requests", "input_tokens", "output_tokens"):
                    if type(r.get(k, 0)) is not int or r.get(k, 0) < 0:
                        raise ValueError("Contatori export non validi")
        windows.append((start, end))
    ordered = sorted(windows)
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("Intervalli duplicati o sovrapposti nello stesso export")
    return payload


def merge_exports(old, new, kind):
    validate_export(old, kind)
    validate_export(new, kind)
    rows = list(old["data"])
    for incoming in new["data"]:
        start, end = incoming["start_time"], incoming.get("end_time", incoming["start_time"] + 3600)
        remaining = []
        for prior in rows:
            a, b = prior["start_time"], prior.get("end_time", prior["start_time"] + 3600)
            if b <= start or a >= end:
                remaining.append(prior)
            elif not start <= a < b <= end:
                raise ValueError("Export parzialmente sovrapposti: usa la stessa granularità temporale")
        rows = [*remaining, incoming]
    return {"object": new.get("object", "page"), "data": sorted(rows, key=lambda b: b["start_time"])}


class OpenAIExports:
    def __init__(self, cost_path, usage_path):
        self.cost_path, self.usage_path = Path(cost_path), Path(usage_path)
        self.pointer = self.cost_path.parent / "export-current.json"

    def read(self):
        if self.pointer.exists():
            pointer = json.loads(self.pointer.read_text(encoding="utf-8"))
            name = pointer["name"]
            if not re.fullmatch(r"[a-f0-9]{64}\.json", name):
                raise ValueError("Riferimento export non valido")
            raw = (self.pointer.parent / "imports" / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != name[:-5]:
                raise ValueError("Integrità export non verificata")
            pair = json.loads(raw)
            return pair["costs"], pair["usage"]
        def load(path):
            return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"data": []}
        return load(self.cost_path), load(self.usage_path)

    def save(self, costs, usage):
        old_costs, old_usage = self.read()
        pair = {"costs": merge_exports(old_costs, costs, "costs"), "usage": merge_exports(old_usage, usage, "usage")}
        raw = json.dumps(pair, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        destination = self.pointer.parent / "imports" / (digest + ".json")
        if destination.exists():
            if destination.read_bytes() != raw:
                raise ValueError("Conflitto nel contenuto export")
        else:
            atomic_write(destination, raw)
        current = json.loads(self.pointer.read_text(encoding="utf-8")) if self.pointer.exists() else {}
        if current.get("name") != destination.name:
            atomic_write(self.pointer, json.dumps({"name": destination.name, "imported_at": datetime.now(UTC).isoformat()}).encode("utf-8"))
        return pair
