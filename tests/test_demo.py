"""The local demo candidate never calls Prodigal."""

from __future__ import annotations

import unittest
from typing import Any

from agent.session import Session
from web.demo import DEMO_ID, LocalDemoAPI


class FakeAudit:
    def __init__(self) -> None:
        self._secrets: list[str] = []

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        return None

    def finish(self, outcome: str | None) -> None:
        return None

    def say(self, role: str, text: str) -> None:
        return None


def _session() -> tuple[Session, LocalDemoAPI]:
    api = LocalDemoAPI()
    session = Session(api, api.get_candidate(DEMO_ID), FakeAudit())
    return session, api


class DemoTests(unittest.TestCase):
    def test_demo_is_pending_and_greets_by_name(self) -> None:
        session, api = _session()
        self.assertEqual(session.api_status(), "pending")
        self.assertIn("Anika Rao", session.greeting())
        self.assertNotIn(DEMO_ID, session.greeting())
        self.assertTrue(session.begin()["chat"])
        self.assertEqual(api.patches, [])

    def test_one_message_saves_several_fields_locally(self) -> None:
        session, api = _session()
        session.identity_confirmed = True
        session.last_candidate_text = (
            "I study B.Tech in Computer Engineering at NMIMS. "
            "My email is anika.demo@example.com."
        )
        result = session.dispatch(
            "record_candidate_fields",
            {
                "fields": [
                    {"field": "email", "value": "anika.demo@example.com", "signal": "clear", "quote": "anika.demo@example.com"},
                    {"field": "degree", "value": "B.Tech", "signal": "clear", "quote": "B.Tech"},
                    {"field": "branch", "value": "Computer Engineering", "signal": "clear", "quote": "Computer Engineering"},
                    {"field": "college", "value": "NMIMS", "signal": "clear", "quote": "NMIMS"},
                ]
            },
        )
        self.assertTrue(result["saved"])
        self.assertEqual(api.record["collection_status"], "in_progress")
        self.assertEqual(api.record["email"], "anika.demo@example.com")
        self.assertNotIn("phone", api.patches[0])
        self.assertEqual(api.record["phone"], "+910000000000")

    def test_invalid_and_ambiguous_values_are_not_saved(self) -> None:
        session, api = _session()
        session.identity_confirmed = True
        session.last_candidate_text = "My CGPA is 85, or around 8 or 9."
        session.dispatch(
            "record_candidate_fields",
            {"fields": [{"field": "cgpa", "value": "85", "signal": "clear", "quote": "85"}]},
        )
        session.last_candidate_text = "around 8 or 9"
        session.dispatch(
            "record_candidate_fields",
            {"fields": [{"field": "cgpa", "value": "around 8 or 9", "signal": "ambiguous", "quote": "around 8 or 9"}]},
        )
        self.assertNotIn("cgpa", api.record)

    def test_optional_refusal_stays_open_and_continue_later_lists_remaining_fields(self) -> None:
        session, api = _session()
        session.identity_confirmed = True
        refused = session.finish("refused", "No GitHub.", ["github_url"])
        self.assertFalse(refused["ok"])
        self.assertEqual(api.patches, [])
        session.last_candidate_text = "anika.demo@example.com"
        session.dispatch(
            "record_candidate_fields",
            {"fields": [{"field": "email", "value": "anika.demo@example.com", "signal": "clear", "quote": "anika.demo@example.com"}]},
        )
        closed = session.finish("in_progress", "Candidate will continue tomorrow.", [])
        self.assertEqual(closed["outcome"], "in_progress")
        self.assertIn("Remaining:", api.patches[-1]["notes"])
        self.assertEqual(api.record["collection_status"], "in_progress")

    def test_no_reply_and_identity_dispute_do_not_touch_prodigal_fields(self) -> None:
        quiet, quiet_api = _session()
        quiet.outcome_for_departure("Candidate typed /quit.")
        self.assertEqual(quiet_api.record["collection_status"], "no_answer")
        self.assertEqual(quiet_api.record["attempt_count"], 1)
        self.assertNotIn("phone", quiet_api.patches[-1])

        disputed, disputed_api = _session()
        disputed.dispatch("confirm_identity", {"confirmed": False, "note": "Wrong person."})
        self.assertEqual(disputed_api.record["collection_status"], "needs_human")
        before = len(disputed_api.patches)
        disputed.dispatch(
            "record_candidate_fields",
            {"fields": [{"field": "email", "value": "anika.demo@example.com", "signal": "clear", "quote": "anika.demo@example.com"}]},
        )
        self.assertEqual(len(disputed_api.patches), before)

    def test_completed_demo_does_not_restart(self) -> None:
        record = initial_completed()
        api = LocalDemoAPI(record)
        session = Session(api, api.get_candidate(DEMO_ID), FakeAudit())
        decision = session.begin()
        self.assertFalse(decision["chat"])
        self.assertEqual(api.patches, [])
        self.assertEqual(session.api_status(), "completed")


def initial_completed() -> dict[str, Any]:
    record = LocalDemoAPI().record
    record.update(
        {
            "collection_status": "completed",
            "email": "anika.demo@example.com",
            "college": "NMIMS",
            "degree": "B.Tech",
            "branch": "Computer Engineering",
            "graduation_year": 2027,
            "cgpa": 8.4,
            "preferred_location": "Mumbai",
            "available_from": "2026-06-01",
            "internship_duration_months": 6,
        }
    )
    return record


if __name__ == "__main__":
    unittest.main()
