"""Structured JSON logging with automatic secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), "***telegram-token***"),
    (re.compile(r"(?i)(authorization|x-api-key|api[_-]?key|token|secret|password|passwd|cookie|session)"
                r"(\"?\s*[:=]\s*\"?)([^\s\"',;&]+)"), r"\1\2***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "***private-key***"),
    # 12/24-word mnemonic-like sequences of lowercase words
    (re.compile(r"\b(?:[a-z]{3,8}\s+){11,23}[a-z]{3,8}\b"), "***possible-seed-phrase***"),
    (re.compile(r"\b(?:0x)?[a-fA-F0-9]{64}\b"), "***hex-secret***"),
    (re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@", re.I), r"\1***:***@"),
]

_extra_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret value so it is always masked if it ever appears in a log line."""
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    for s in _extra_secrets:
        text = text.replace(s, "***")
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k.startswith("ctx_"):
                payload[k[4:]] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(RedactingJsonFormatter() if json_logs else
                   RedactingTextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(h)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel("WARNING")
