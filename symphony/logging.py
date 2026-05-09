from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .models import now_beijing


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": now_beijing().isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        for key in ("issue_id", "issue_identifier", "session_id", "event", "error"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("symphony")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
