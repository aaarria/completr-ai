"""Local checks for field validation. These do not call the API."""

from __future__ import annotations

import unittest

from services.validation import (
    INVALID,
    MISSING,
    NEEDS_CLARIFICATION,
    VALID,
    FieldResult,
    all_mandatory_valid,
    assess_record,
    judge_extraction,
    outstanding,
)


def _complete_record() -> dict:
    return {
        "full_name": "Priya Shah",
        "phone": "+910000000000",
        "email": "priya@example.com",
        "college": "NMIMS",
        "degree": "B.Tech",
        "branch": "Computer Engineering",
        "graduation_year": 2027,
        "cgpa": 8.6,
        "preferred_location": "Mumbai",
        "available_from": "2026-06-01",
        "internship_duration_months": 6,
    }


class ValidationTests(unittest.TestCase):
    def test_complete_record_needs_no_chat(self) -> None:
        results = assess_record(_complete_record())
        self.assertTrue(all_mandatory_valid(results))
        self.assertEqual(outstanding(results), [])

    def test_optional_fields_do_not_block(self) -> None:
        record = _complete_record()
        self.assertTrue(all_mandatory_valid(assess_record(record)))

    def test_multi_field_sentence_is_judged_independently(self) -> None:
        extracted = [
            ("degree", "B.Tech", VALID),
            ("branch", "Computer Engineering", VALID),
            ("college", "NMIMS", VALID),
            ("graduation_year", "2027", VALID),
            ("cgpa", "8.6", VALID),
            ("cgpa", "85", INVALID),
        ]
        # The last cgpa replaces the check below; judge each against a missing field.
        statuses = []
        for field, value, _expected in extracted:
            result = judge_extraction(field, value, "clear", False, FieldResult(field, MISSING))
            statuses.append((field, value, result.status))
        self.assertEqual(
            statuses,
            [
                ("degree", "B.Tech", VALID),
                ("branch", "Computer Engineering", VALID),
                ("college", "NMIMS", VALID),
                ("graduation_year", "2027", VALID),
                ("cgpa", "8.6", VALID),
                ("cgpa", "85", INVALID),
            ],
        )

    def test_ambiguous_cgpa_is_not_stored(self) -> None:
        result = judge_extraction("cgpa", "around 8 or 9", "clear", False, FieldResult("cgpa", MISSING))
        self.assertEqual(result.status, NEEDS_CLARIFICATION)

    def test_explicit_cgpa_is_stored_to_two_decimals(self) -> None:
        result = judge_extraction("cgpa", "8.6", "clear", False, FieldResult("cgpa", MISSING))
        self.assertEqual(result.status, VALID)
        self.assertEqual(result.value, 8.6)

    def test_existing_invalid_cgpa(self) -> None:
        record = _complete_record()
        record["cgpa"] = 85
        result = assess_record(record)["cgpa"]
        self.assertEqual(result.status, INVALID)

    def test_aliases_and_bounds(self) -> None:
        self.assertEqual(
            judge_extraction("degree", "btech", "clear", False, FieldResult("degree", MISSING)).value,
            "B.Tech",
        )
        self.assertEqual(
            judge_extraction(
                "preferred_location",
                "Bangalore",
                "clear",
                False,
                FieldResult("preferred_location", MISSING),
            ).value,
            "Bengaluru",
        )
        year = judge_extraction(
            "graduation_year", "2025", "clear", False, FieldResult("graduation_year", MISSING)
        )
        self.assertEqual(year.status, INVALID)
        too_early = judge_extraction(
            "available_from", "2025-12-31", "clear", False, FieldResult("available_from", MISSING)
        )
        self.assertEqual(too_early.status, INVALID)
        duration = judge_extraction(
            "internship_duration_months",
            "six months",
            "clear",
            False,
            FieldResult("internship_duration_months", MISSING),
        )
        self.assertEqual(duration.value, 6)

    def test_valid_value_is_not_replaced_without_confirmation(self) -> None:
        current = FieldResult("email", VALID, value="priya@example.com")
        result = judge_extraction("email", "other@example.com", "clear", False, current)
        self.assertEqual(result.status, NEEDS_CLARIFICATION)
        confirmed = judge_extraction("email", "other@example.com", "clear", True, current)
        self.assertEqual(confirmed.status, VALID)
        self.assertEqual(confirmed.value, "other@example.com")

    def test_phone_cannot_be_updated(self) -> None:
        result = judge_extraction("phone", "+911111111111", "clear", True, FieldResult("phone", VALID, value="+910000000000"))
        self.assertEqual(result.status, INVALID)

    def test_bad_email_and_short_college(self) -> None:
        self.assertEqual(
            judge_extraction("email", "not-an-email", "clear", False, FieldResult("email", MISSING)).status,
            INVALID,
        )
        self.assertEqual(
            judge_extraction("college", "N", "clear", False, FieldResult("college", MISSING)).status,
            INVALID,
        )


if __name__ == "__main__":
    unittest.main()
