"""Status handling with a fake Prodigal API. These tests do not write to the live database."""

from __future__ import annotations

import unittest
from typing import Any

from agent.session import ALREADY_COMPLETED_MESSAGE, Session


class FakeAudit:
    def __init__(self) -> None:
        self._secrets: list[str] = []

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        return None

    def finish(self, outcome: str | None) -> None:
        return None

    def say(self, role: str, text: str) -> None:
        return None


class FakeAPI:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = dict(record)
        self.patches: list[dict[str, Any]] = []
        self.reads = 0

    def update_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any],
        version: Any,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.patches.append(dict(payload))
        merged = dict(self.record)
        merged.update(payload)
        merged["id"] = candidate_id
        merged["version"] = int(self.record.get("version") or 1) + 1
        self.record = merged
        return dict(merged)

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        self.reads += 1
        return dict(self.record)


class StaleReadAPI(FakeAPI):
    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        self.reads += 1
        stale = dict(self.record)
        stale["collection_status"] = "pending"
        return stale


def _record(status: str, *, email: str | None = "priya@example.com") -> dict[str, Any]:
    record = {
        "id": "c010",
        "version": 4,
        "collection_status": status,
        "attempt_count": 1,
        "full_name": "Priya Shah",
        "phone": "+910000000000",
        "email": email,
        "college": "NMIMS",
        "degree": "B.Tech",
        "branch": "Computer Engineering",
        "graduation_year": 2027,
        "cgpa": 8.6,
        "preferred_location": "Mumbai",
        "available_from": "2026-06-01",
        "internship_duration_months": 6,
    }
    if email is None:
        record.pop("email")
    return record


def _begin(record: dict[str, Any], api: FakeAPI | None = None) -> tuple[Session, FakeAPI]:
    api = api or FakeAPI(record)
    session = Session(api, record, FakeAudit())
    session.begin()
    return session, api


class StatusTests(unittest.TestCase):
    def test_completed_record_needs_no_conversation_and_no_write(self) -> None:
        session, api = _begin(_record("completed"))
        self.assertEqual(session.closing_line, ALREADY_COMPLETED_MESSAGE)
        self.assertTrue(session.stop)
        self.assertEqual(api.patches, [])
        state = session.snapshot()
        self.assertEqual(state["collection_status"], "completed")
        self.assertFalse(state["chat_enabled"])
        self.assertFalse(state["start_enabled"])
        self.assertEqual(state["disabled_reason"], ALREADY_COMPLETED_MESSAGE)

    def test_completed_status_wins_even_when_a_field_is_missing(self) -> None:
        session, api = _begin(_record("completed", email=None))
        self.assertEqual(session.closing_line, ALREADY_COMPLETED_MESSAGE)
        self.assertEqual(api.patches, [])
        self.assertEqual(session.snapshot()["collection_status"], "completed")

    def test_pending_and_valid_completes_through_patch_and_fresh_get(self) -> None:
        session, api = _begin(_record("pending"))
        self.assertEqual(len(api.patches), 1)
        body = api.patches[0]
        self.assertEqual(body["collection_status"], "completed")
        self.assertIn("All mandatory fields are valid", body["notes"])
        self.assertNotIn("phone", body)
        self.assertNotIn("attempt_count", body)
        self.assertGreaterEqual(api.reads, 1)
        self.assertEqual(session.api_status(), "completed")
        self.assertEqual(session.outcome, "completed")
        self.assertNotIn("already completed", session.closing_line.lower())
        state = session.snapshot()
        self.assertFalse(state["chat_enabled"])
        self.assertFalse(state["start_enabled"])
        self.assertIn("now completed", state["disabled_reason"])

    def test_unverified_completion_does_not_claim_the_status_changed(self) -> None:
        record = _record("pending")
        api = StaleReadAPI(record)
        session, _ = _begin(record, api)
        self.assertEqual(api.patches[0]["collection_status"], "completed")
        self.assertIsNone(session.outcome)
        self.assertEqual(session.api_status(), "pending")
        self.assertIn("could not be verified", session.closing_line)
        self.assertFalse(session.snapshot()["chat_enabled"])

    def test_pending_with_a_missing_field_keeps_the_composer_open(self) -> None:
        session, api = _begin(_record("pending", email=None))
        self.assertEqual(api.patches, [])
        self.assertFalse(session.stop)
        state = session.snapshot()
        self.assertEqual(state["collection_status"], "pending")
        self.assertTrue(state["chat_enabled"])
        self.assertTrue(state["start_enabled"])
        self.assertEqual(state["action_label"], "Start")
        self.assertEqual(state["disabled_reason"], "")

    def test_in_progress_with_a_missing_field_can_resume(self) -> None:
        session, api = _begin(_record("in_progress", email=None))
        self.assertEqual(api.patches, [])
        state = session.snapshot()
        self.assertEqual(state["collection_status"], "in_progress")
        self.assertTrue(state["chat_enabled"])
        self.assertEqual(state["action_label"], "Resume")

    def test_valid_in_progress_record_is_not_overwritten(self) -> None:
        session, api = _begin(_record("in_progress"))
        self.assertEqual(api.patches, [])
        self.assertEqual(session.api_status(), "in_progress")
        self.assertIn("not changed", session.closing_line)
        self.assertFalse(session.snapshot()["chat_enabled"])

    def test_terminal_statuses_are_not_overwritten(self) -> None:
        for status in ("refused", "no_answer", "needs_human"):
            with self.subTest(status=status):
                session, api = _begin(_record(status, email=None))
                self.assertEqual(api.patches, [])
                self.assertEqual(session.api_status(), status)
                self.assertIn(status, session.closing_line)
                self.assertIn("not changed", session.closing_line)
                self.assertFalse(session.snapshot()["chat_enabled"])


if __name__ == "__main__":
    unittest.main()
