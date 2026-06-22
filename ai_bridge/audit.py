from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    """Append-only JSONL audit log with prompt/secret-free event payloads."""

    def __init__(self, path: str | None, *, enabled: bool = True):
        self.enabled = enabled
        self.path = Path(path).expanduser() if path and enabled else None
        self._lock = threading.RLock()
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch(exist_ok=True)
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "audit logging disabled: could not open configured audit path %s (%s: %s)",
                    self.path,
                    exc.__class__.__name__,
                    exc,
                )
                self.path = None

    def emit(self, event_type: str, *, outcome: str, **fields: Any) -> None:
        if not self.enabled or self.path is None:
            return
        safe = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "outcome": outcome,
        }
        for key, value in fields.items():
            if value is None:
                continue
            if key.lower() in {"prompt", "api_key", "token", "secret", "authorization"}:
                continue
            safe[key] = value
        line = json.dumps(safe, sort_keys=True, separators=(",", ":"))
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "audit event dropped: could not write audit path %s (%s: %s)",
                self.path,
                exc.__class__.__name__,
                exc,
            )
            self.path = None
