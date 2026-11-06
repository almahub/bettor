"""Persist actionable failures without credentials or raw request bodies."""

import os
import re
from urllib.parse import urlsplit


def safe_message(value):
    text = str(value)
    for name, secret in os.environ.items():
        if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)bearer\s+[^\s,;'\"}]+", "Bearer [redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)((?:[\w-]*(?:api[_-]?key|token|secret|password)|x-apisports-key|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s,;&\"'}]+",
        r"\1[redacted]", text,
    )
    def clean_url(match):
        try:
            url = urlsplit(match[0])
            return f"{url.scheme}://{url.hostname or '[redacted]'}{url.path}"
        except ValueError:
            return "[redacted URL]"

    text = re.sub(r"https?://[^\s'\"]+", clean_url, text)
    text = re.sub(r"\b[a-fA-F0-9]{32,}\b", "[redacted]", text)
    return text[:600]


def failure_detail(error, *, match_id=None, checkpoint=None, provider=None, stage=None):
    detail = getattr(error, "detail", None)
    response = getattr(error, "response", None)
    http_code = re.search(r"\bHTTP(?: status)?\s*[:=]?\s*(\d{3})\b", str(error), re.IGNORECASE)
    return {
        "code": str(getattr(detail, "code", None) or type(error).__name__),
        "message": safe_message(getattr(detail, "message", None) or str(error)),
        "provider": safe_message(getattr(detail, "source", None) or provider) if (getattr(detail, "source", None) or provider) else None,
        "match_id": match_id,
        "checkpoint": checkpoint,
        "stage": stage,
        "http_status": getattr(response, "status_code", None) or (int(http_code[1]) if http_code else None),
        "retryable": bool(getattr(detail, "retryable", False)),
        "exception_type": type(error).__name__,
    }
