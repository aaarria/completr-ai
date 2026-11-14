"""JSON audit trail for one Completr AI session.

The file records decisions, tool calls, validation, and API payloads.
API keys and phone numbers are removed before anything is written.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
_HIDDEN_KEYS = {"phone", "authorization", "api_key", "x-goog-api-key", "gemini_api_key"}


class AuditLog:
    def __init__(self, candidate_id: str, secrets: list[str]) -> None:
        self.candidate_id = candidate_id
        self._secrets = [secret for secret in secrets if secret]
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        self.path = RUNS_DIR / f"{candidate_id}_{stamp}_{uuid4().hex[:6]}.json"
        self.data: dict[str, Any] = {
            "product": "Completr AI",
            "candidate_id": candidate_id,
            "started_at": _now(),
            "events": [],
            "transcript": [],
            "final_outcome": None,
        }
        self.write()

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        self.data["events"].append(
            {
                "at": _now(),
                "type": kind,
                "payload": self._scrub(payload),
            }
        )
        self.write()

    def say(self, role: str, text: str) -> None:
        self.data["transcript"].append({"at": _now(), "role": role, "text": self._scrub(text)})
        self.write()

    def finish(self, outcome: str | None) -> None:
        self.data["final_outcome"] = outcome
        self.data["finished_at"] = _now()
        self.write()

    def write(self) -> None:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        temporary.replace(self.path)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                if str(key).lower() in _HIDDEN_KEYS:
                    cleaned[key] = "[redacted]"
                else:
                    cleaned[key] = self._scrub(item)
            return cleaned
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, str):
            redacted = value
            for secret in self._secrets:
                redacted = redacted.replace(secret, "[redacted]")
            return redacted
        return value


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
