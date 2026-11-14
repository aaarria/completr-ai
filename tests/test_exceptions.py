"""Exception outcomes against a fake database. These tests do not call Prodigal."""

from __future__ import annotations

import unittest
from typing import Any

from agent.session import Session


class FakeAudit:
    def __init__(self) -> None:
        self._secrets: list[str] = []
        self.events: list[tuple[str, Any]] = []

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    def finish(self, outcome: str | None) -> None:
        self.events.append(("finish", outcome))

    def say(self, role: str, text: str) -> None:
        return None


class FakeAPI:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = dict(record)
        self.patches: list[dict[str, Any]] = []

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
        merged["version"] = int(self.record.get("version") or 1) + len(self.patches)
        self.record = merged
        return dict(merged)

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        return dict(self.record)


def _open_record() -> dict[str, Any]:
    return {
        "id": "c099",
        "version": 1,
        "collection_status": "pending",
        "attempt_count": 2,
        "full_name": "Test Candidate",
        "phone": "+910000000000",
        "college": "NMIMS",
        "degree": "B.Tech",
        "branch": "Computer Engineering",
        "graduation_year": 2027,
        "preferred_location": "Mumbai",
        "available_from": "2026-06-01",
        "internship_duration_months": 6,
    }


def _session() -> tuple[Session, FakeAPI]:
    record = _open_record()
    api = FakeAPI(record)
    return Session(api, record, FakeAudit()), api


class ExceptionTests(unittest.TestCase):
    def test_continue_later_saves_progress_and_lists_remaining_fields(self) -> None:
        session, api = _session()
        session.identity_confirmed = True
        session.last_candidate_text = "My email is test.candidate@example.com"
        saved = session.dispatch(
            "record_candidate_fields",
            {
                "fields": [
                    {
                        "field": "email",
                        "value": "test.candidate@example.com",
                        "signal": "clear",
                        "quote": "test.candidate@example.com",
                    }
                ]
            },
        )
        self.assertTrue(saved["saved"])
        progress = api.patches[0]
        self.assertEqual(progress["collection_status"], "in_progress")
        self.assertEqual(progress["email"], "test.candidate@example.com")
        self.assertIn("cgpa", progress["notes"])
        self.assertNotIn("phone", progress)

        closed = session.finish("in_progress", "Candidate will continue tomorrow.", [])
        self.assertEqual(closed["outcome"], "in_progress")
        self.assertTrue(session.stop)
        final = api.patches[-1]
        self.assertEqual(final["collection_status"], "in_progress")
        self.assertIn("Remaining: cgpa", final["notes"])
        self.assertNotIn("phone", final)

    def test_mandatory_refusal_records_the_field(self) -> None:
        session, api = _session()
        result = session.finish("refused", "Candidate refused to share CGPA.", ["cgpa"])
        self.assertEqual(result["outcome"], "refused")
        self.assertTrue(session.stop)
        body = api.patches[-1]
        self.assertEqual(body["collection_status"], "refused")
        self.assertIn("Refused: cgpa", body["notes"])
        self.assertNotIn("phone", body)

        before = len(api.patches)
        blocked = session.dispatch(
            "record_candidate_fields",
            {
                "fields": [
                    {
                        "field": "email",
                        "value": "test.candidate@example.com",
                        "signal": "clear",
                        "quote": "test.candidate@example.com",
                    }
                ]
            },
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(len(api.patches), before)

    def test_optional_refusal_does_not_close_the_application(self) -> None:
        session, api = _session()
        result = session.finish("refused", "No GitHub.", ["github_url"])
        self.assertFalse(result["ok"])
        self.assertIsNone(session.outcome)
        self.assertEqual(api.patches, [])

    def test_no_response_increments_attempt_count(self) -> None:
        session, api = _session()
        result = session.outcome_for_departure("Candidate typed /quit.")
        self.assertEqual(result["outcome"], "no_answer")
        body = api.patches[-1]
        self.assertEqual(body["collection_status"], "no_answer")
        self.assertEqual(body["attempt_count"], 3)
        self.assertNotIn("email", body)
        self.assertNotIn("phone", body)

    def test_identity_dispute_stops_later_updates(self) -> None:
        session, api = _session()
        result = session.dispatch("confirm_identity", {"confirmed": False, "note": "Wrong person."})
        self.assertEqual(result["outcome"], "needs_human")
        self.assertTrue(session.stop)
        self.assertEqual(api.patches[-1]["collection_status"], "needs_human")
        before = len(api.patches)
        blocked = session._record_fields(
            {
                "fields": [
                    {
                        "field": "email",
                        "value": "test.candidate@example.com",
                        "signal": "clear",
                        "quote": "test.candidate@example.com",
                    }
                ]
            }
        )
        self.assertEqual(blocked["outcome"], "needs_human")
        self.assertEqual(len(api.patches), before)

    def test_unresolved_contradiction_escalates_without_saving_the_value(self) -> None:
        session, api = _session()
        session.identity_confirmed = True
        session.last_candidate_text = "around 8 or 9"
        fields = {
            "fields": [
                {
                    "field": "cgpa",
                    "value": "around 8 or 9",
                    "signal": "ambiguous",
                    "quote": "around 8 or 9",
                }
            ]
        }
        first = session.dispatch("record_candidate_fields", fields)
        self.assertNotEqual(first.get("outcome"), "needs_human")
        second = session.dispatch("record_candidate_fields", fields)
        self.assertEqual(second["outcome"], "needs_human")
        self.assertTrue(session.stop)
        for patch in api.patches:
            self.assertNotIn("cgpa", patch)
        self.assertEqual(api.patches[-1]["collection_status"], "needs_human")
        before = len(api.patches)
        session.dispatch("record_candidate_fields", fields)
        self.assertEqual(len(api.patches), before)


if __name__ == "__main__":
    unittest.main()
