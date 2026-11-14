"""An in-memory candidate for demonstrations. Nothing here is sent to Prodigal."""

from __future__ import annotations

from typing import Any

DEMO_ID = "demo"


def initial_demo_record() -> dict[str, Any]:
    """A labelled practice record with most mandatory answers still missing."""
    return {
        "id": DEMO_ID,
        "version": 1,
        "collection_status": "pending",
        "attempt_count": 0,
        "full_name": "Anika Rao",
        "phone": "+910000000000",
        "notes": "Local demonstration record. Not stored in Prodigal.",
        "last_contacted_at": "",
    }


class LocalDemoAPI:
    """The same methods Session uses, backed by one local dictionary."""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self.record = dict(record or initial_demo_record())
        self.patches: list[dict[str, Any]] = []

    def has_api_key(self) -> bool:
        return True

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        if str(candidate_id).strip() != DEMO_ID:
            raise KeyError(candidate_id)
        return dict(self.record)

    def update_candidate(
        self,
        candidate_id: str,
        fields: dict[str, Any],
        version: Any,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if str(candidate_id).strip() != DEMO_ID:
            raise KeyError(candidate_id)
        body = {
            key: value
            for key, value in fields.items()
            if key not in {"id", "phone", "version", "updated_at"} and value is not None
        }
        self.patches.append(dict(body))
        merged = dict(self.record)
        merged.update(body)
        merged["id"] = DEMO_ID
        merged["version"] = int(self.record.get("version") or 1) + 1
        self.record = merged
        return dict(merged)

    def close(self) -> None:
        return None
