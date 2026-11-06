"""Minimal exact-name .env reader; never interpolates or logs secret values."""

import os
from pathlib import Path


def environment_value(name, path=Path(".env")):
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() == name:
            return raw.strip().strip('"').strip("'")
    return ""
