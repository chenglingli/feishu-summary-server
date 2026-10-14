"""Allowlisted metadata only; never log external responses, exceptions or payloads."""

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger("summary.runner")
logger.addHandler(logging.NullHandler())


def configure():
    if not any(type(handler) is logging.StreamHandler for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def event(name, **fields):
    from app.leases import current_lease

    record = {"time": datetime.now(timezone.utc).isoformat(), "event": name}
    lease = current_lease.get()
    if lease:
        fields.setdefault("run_id", lease[0])
    for key, value in fields.items():
        # Ignore unknown fields and arbitrary text even if accidentally passed by a caller.
        if key in {"run_id", "call_id", "stage", "code", "kind", "error_type", "coverage"}:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                record[key] = value
        elif key in {
            "reserved_tokens",
            "budget_used",
            "budget_limit",
            "input_chars",
            "output_chars",
            "max_output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "duration_ms",
            "attempt",
            "part",
            "parts",
            "active_tasks",
            "http_status",
            "phase",
            "unique",
            "body_read",
            "body_attempted",
            "parsed",
            "context",
            "unsupported",
            "retry_count",
        }:
            if type(value) is int:
                record[key] = value
    # StreamHandler flushes every record, including when redirected to runner.log.
    logger.info(json.dumps(record, ensure_ascii=False))
