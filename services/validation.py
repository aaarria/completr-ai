"""Deterministic checks for ACME internship applications.

Gemini may extract words from a chat message. Only this module decides
whether a value is stored. Optional LinkedIn, GitHub, and skills never
block completion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

VALID = "VALID"
MISSING = "MISSING"
INVALID = "INVALID"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"

MANDATORY_FIELDS = [
    "full_name",
    "phone",
    "email",
    "college",
    "degree",
    "branch",
    "graduation_year",
    "cgpa",
    "preferred_location",
    "available_from",
    "internship_duration_months",
]

OPTIONAL_FIELDS = ["linkedin_url", "github_url", "skills"]
PROFILE_FIELDS = [field for field in MANDATORY_FIELDS if field != "phone"] + OPTIONAL_FIELDS

DEGREES = ["B.Tech", "B.E.", "B.Sc", "BCA", "MBA Tech", "MCA", "M.Tech", "Other"]
LOCATIONS = ["Mumbai", "Bengaluru", "Remote", "No preference"]
DURATIONS = [2, 3, 6]
DATE_MIN = date(2026, 1, 1)
DATE_MAX = date(2027, 12, 31)

_DEGREE_ALIASES = {
    "btech": "B.Tech",
    "b tech": "B.Tech",
    "bachelor of technology": "B.Tech",
    "be": "B.E.",
    "bachelor of engineering": "B.E.",
    "bsc": "B.Sc",
    "b sc": "B.Sc",
    "bachelor of science": "B.Sc",
    "bca": "BCA",
    "bachelor of computer applications": "BCA",
    "mba tech": "MBA Tech",
    "mbatech": "MBA Tech",
    "mtech": "M.Tech",
    "m tech": "M.Tech",
    "master of technology": "M.Tech",
    "mca": "MCA",
    "master of computer applications": "MCA",
    "other": "Other",
}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_AMBIGUOUS = (
    "around",
    "about",
    "approx",
    "roughly",
    "maybe",
    "between",
    "not sure",
    "don't know",
    "dont know",
    "idk",
    "something like",
    "or so",
    "almost",
    "nearly",
    "sometime",
    "not certain",
    "i think",
    "probably",
    "either",
)


@dataclass
class FieldResult:
    field: str
    status: str
    value: Any = None
    reason: str = ""


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    return False


def assess_record(record: dict[str, Any]) -> dict[str, FieldResult]:
    """Classify every mandatory and optional field already stored on a record."""
    return {
        field: validate_field(field, record.get(field), from_chat=False)
        for field in MANDATORY_FIELDS + OPTIONAL_FIELDS
    }


def all_mandatory_valid(results: dict[str, FieldResult]) -> bool:
    return all(results[field].status == VALID for field in MANDATORY_FIELDS)


def progress_count(results: dict[str, FieldResult]) -> tuple[int, int]:
    valid = sum(1 for field in MANDATORY_FIELDS if results[field].status == VALID)
    return valid, len(MANDATORY_FIELDS)


def outstanding(results: dict[str, FieldResult]) -> list[FieldResult]:
    return [results[field] for field in MANDATORY_FIELDS if results[field].status != VALID]


def validate_field(field: str, value: Any, from_chat: bool) -> FieldResult:
    """Return the status of one field. `from_chat` turns vague answers into clarifications."""
    checkers = {
        "full_name": _name,
        "phone": _phone,
        "email": _email,
        "college": _college,
        "degree": _degree,
        "branch": _branch,
        "graduation_year": _year,
        "cgpa": _cgpa,
        "preferred_location": _location,
        "available_from": _available_from,
        "internship_duration_months": _duration,
        "linkedin_url": _linkedin,
        "github_url": _github,
        "skills": _skills,
    }
    checker = checkers.get(field)
    if checker is None:
        return FieldResult(field, INVALID, reason="That field cannot be collected.")
    return checker(value, from_chat)


def judge_extraction(
    field: str,
    raw_value: Any,
    signal: str,
    replaces_existing: bool,
    current: FieldResult,
) -> FieldResult:
    """Decide what to do with one extracted field.

    A valid value already on the record is kept unless the candidate
    explicitly confirms a replacement. Ambiguous answers are never stored.
    """
    if field == "phone" or field not in PROFILE_FIELDS:
        return FieldResult(field, INVALID, reason="Phone and read-only fields cannot be updated.")

    if signal == "ambiguous" or (signal == "contradicts_previous" and not replaces_existing):
        reason = (
            "The answer conflicts with information already on the application. "
            "Ask which value to keep."
            if signal == "contradicts_previous"
            else "The answer is ambiguous. Ask for one exact value."
        )
        return FieldResult(field, NEEDS_CLARIFICATION, reason=reason)

    proposed = validate_field(field, raw_value, from_chat=True)
    if proposed.status != VALID:
        return proposed

    if current.status == VALID and not same_value(field, current.value, proposed.value):
        if not replaces_existing:
            return FieldResult(
                field,
                NEEDS_CLARIFICATION,
                value=proposed.value,
                reason=(
                    "A different valid value is already stored. "
                    "Ask the candidate to confirm the change before replacing it."
                ),
            )
    return proposed


def same_value(field: str, left: Any, right: Any) -> bool:
    if field == "cgpa":
        try:
            return abs(float(left) - float(right)) < 0.001
        except (TypeError, ValueError):
            return False
    if field == "skills":
        return _skill_key(left) == _skill_key(right)
    return str(left).strip() == str(right).strip()


def _ambiguous(text: str) -> bool:
    lowered = f" {text.lower()} "
    if any(marker in lowered for marker in _AMBIGUOUS):
        return True
    if lowered.strip().endswith("ish"):
        return True
    return False


def _missing_or_text(field: str, value: Any, from_chat: bool) -> tuple[str | None, FieldResult | None]:
    if is_blank(value):
        return None, FieldResult(field, MISSING, reason=f"{field} is missing.")
    text = str(value).strip()
    if from_chat and (_ambiguous(text) or " or " in text.lower()):
        return None, FieldResult(
            field,
            NEEDS_CLARIFICATION,
            reason=f"{field.replace('_', ' ')} needs one exact answer.",
        )
    return text, None


def _name(value: Any, from_chat: bool) -> FieldResult:
    text, early = _missing_or_text("full_name", value, from_chat)
    if early:
        return early
    assert text is not None
    if len(text) < 2 or len(text) > 100:
        return FieldResult("full_name", INVALID, reason="Full name must be 2 to 100 characters.")
    if re.search(r"\d", text) or not re.search(r"[A-Za-z]", text):
        return FieldResult("full_name", INVALID, reason="Full name must use letters, not numbers.")
    return FieldResult("full_name", VALID, value=re.sub(r"\s+", " ", text))


def _phone(value: Any, from_chat: bool) -> FieldResult:
    del from_chat
    if is_blank(value):
        return FieldResult("phone", MISSING, reason="Phone is missing and cannot be collected in chat.")
    return FieldResult("phone", VALID, value=str(value).strip())


def _email(value: Any, from_chat: bool) -> FieldResult:
    text, early = _missing_or_text("email", value, from_chat)
    if early:
        return early
    assert text is not None
    if len(text) > 254 or not _EMAIL_RE.fullmatch(text):
        return FieldResult("email", INVALID, reason="Email must be a valid address, up to 254 characters.")
    return FieldResult("email", VALID, value=text)


def _college(value: Any, from_chat: bool) -> FieldResult:
    text, early = _missing_or_text("college", value, from_chat)
    if early:
        return early
    assert text is not None
    if len(text) < 2 or len(text) > 150:
        return FieldResult("college", INVALID, reason="College must be 2 to 150 characters.")
    return FieldResult("college", VALID, value=text)


def _branch(value: Any, from_chat: bool) -> FieldResult:
    text, early = _missing_or_text("branch", value, from_chat)
    if early:
        return early
    assert text is not None
    if len(text) < 2 or len(text) > 100:
        return FieldResult("branch", INVALID, reason="Branch must be 2 to 100 characters.")
    return FieldResult("branch", VALID, value=text)


def _degree(value: Any, from_chat: bool) -> FieldResult:
    text, early = _missing_or_text("degree", value, from_chat)
    if early:
        return early
    assert text is not None
    for allowed in DEGREES:
        if text.lower() == allowed.lower():
            return FieldResult("degree", VALID, value=allowed)
    key = re.sub(r"\s+", " ", text.lower().replace(".", "")).strip()
    mapped = _DEGREE_ALIASES.get(key)
    if mapped:
        return FieldResult("degree", VALID, value=mapped)
    allowed = ", ".join(DEGREES)
    return FieldResult("degree", INVALID, reason=f"Degree must be one of: {allowed}.")


def _year(value: Any, from_chat: bool) -> FieldResult:
    if is_blank(value):
        return FieldResult("graduation_year", MISSING, reason="Graduation year is missing.")
    text = str(value).strip()
    if from_chat and _ambiguous(text):
        return FieldResult(
            "graduation_year",
            NEEDS_CLARIFICATION,
            reason="Graduation year is ambiguous. Ask for a single year.",
        )
    years = re.findall(r"\b20\d{2}\b", text)
    if len(years) != 1:
        status = NEEDS_CLARIFICATION if from_chat else INVALID
        return FieldResult("graduation_year", status, reason="Graduation year must be a single year.")
    year = int(years[0])
    if year < 2026 or year > 2028:
        return FieldResult(
            "graduation_year",
            INVALID,
            reason="Graduation year must be 2026, 2027, or 2028.",
        )
    return FieldResult("graduation_year", VALID, value=year)


def _cgpa(value: Any, from_chat: bool) -> FieldResult:
    if is_blank(value):
        return FieldResult("cgpa", MISSING, reason="CGPA is missing.")
    text = str(value).strip()
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if from_chat and (_ambiguous(text) or len(numbers) != 1):
        return FieldResult(
            "cgpa",
            NEEDS_CLARIFICATION,
            reason="CGPA must be one exact number on a 10-point scale.",
        )
    if len(numbers) != 1:
        return FieldResult("cgpa", INVALID, reason="CGPA is not a number.")
    try:
        number = Decimal(numbers[0])
    except InvalidOperation:
        return FieldResult("cgpa", INVALID, reason="CGPA is not a number.")
    if number < 0 or number > 10:
        return FieldResult("cgpa", INVALID, reason="CGPA must be between 0 and 10.")
    quantized = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return FieldResult("cgpa", VALID, value=float(quantized))


def _location(value: Any, from_chat: bool) -> FieldResult:
    text, early = _missing_or_text("preferred_location", value, from_chat)
    if early:
        return early
    assert text is not None
    lowered = text.lower()
    for allowed in LOCATIONS:
        if lowered == allowed.lower():
            return FieldResult("preferred_location", VALID, value=allowed)
    if "bengaluru" in lowered or "bangalore" in lowered:
        return FieldResult("preferred_location", VALID, value="Bengaluru")
    if "mumbai" in lowered or "bombay" in lowered:
        return FieldResult("preferred_location", VALID, value="Mumbai")
    if "remote" in lowered or "work from home" in lowered or lowered == "wfh":
        return FieldResult("preferred_location", VALID, value="Remote")
    if "no preference" in lowered or lowered in {"any", "anywhere"}:
        return FieldResult("preferred_location", VALID, value="No preference")
    allowed = ", ".join(LOCATIONS)
    return FieldResult("preferred_location", INVALID, reason=f"Location must be one of: {allowed}.")


def _available_from(value: Any, from_chat: bool) -> FieldResult:
    if is_blank(value):
        return FieldResult("available_from", MISSING, reason="Available-from date is missing.")
    parsed: date | None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()
        if from_chat and _ambiguous(text):
            return FieldResult(
                "available_from",
                NEEDS_CLARIFICATION,
                reason="Available-from date is ambiguous. Ask for one YYYY-MM-DD date.",
            )
        parsed = _parse_date(text)
        if parsed is None:
            status = NEEDS_CLARIFICATION if from_chat else INVALID
            return FieldResult(
                "available_from",
                status,
                reason="Available-from must be a real date, preferably YYYY-MM-DD.",
            )
    if parsed < DATE_MIN or parsed > DATE_MAX:
        return FieldResult(
            "available_from",
            INVALID,
            reason="Available-from must be between 2026-01-01 and 2027-12-31.",
        )
    return FieldResult("available_from", VALID, value=parsed.isoformat())


def _duration(value: Any, from_chat: bool) -> FieldResult:
    if is_blank(value):
        return FieldResult("internship_duration_months", MISSING, reason="Internship duration is missing.")
    text = str(value).strip().lower()
    if from_chat and (_ambiguous(text) or " or " in text):
        return FieldResult(
            "internship_duration_months",
            NEEDS_CLARIFICATION,
            reason="Duration must be exactly 2, 3, or 6 months.",
        )
    text = text.replace("months", "").replace("month", "").strip()
    words = {"2": 2, "3": 3, "6": 6, "two": 2, "three": 3, "six": 6}
    if text not in words:
        return FieldResult(
            "internship_duration_months",
            INVALID,
            reason="Internship duration must be 2, 3, or 6 months.",
        )
    return FieldResult("internship_duration_months", VALID, value=words[text])


def _linkedin(value: Any, from_chat: bool) -> FieldResult:
    return _profile_url("linkedin_url", value, from_chat, "linkedin.com")


def _github(value: Any, from_chat: bool) -> FieldResult:
    return _profile_url("github_url", value, from_chat, "github.com")


def _profile_url(field: str, value: Any, from_chat: bool, host: str) -> FieldResult:
    if is_blank(value):
        return FieldResult(field, MISSING, reason=f"{field} is optional and missing.")
    text = str(value).strip()
    if from_chat and _ambiguous(text):
        return FieldResult(field, NEEDS_CLARIFICATION, reason=f"{field} is ambiguous.")
    lowered = text.lower()
    if host not in lowered or " " in text:
        return FieldResult(field, INVALID, reason=f"{field} must be a {host} URL.")
    if not lowered.startswith("http://") and not lowered.startswith("https://"):
        text = "https://" + text
    return FieldResult(field, VALID, value=text)


def _skills(value: Any, from_chat: bool) -> FieldResult:
    if is_blank(value):
        return FieldResult("skills", MISSING, reason="Skills are optional and missing.")
    if isinstance(value, str):
        if from_chat and _ambiguous(value):
            return FieldResult("skills", NEEDS_CLARIFICATION, reason="Skills are ambiguous.")
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        return FieldResult("skills", INVALID, reason="Skills must be a list of short phrases.")
    if not items:
        return FieldResult("skills", MISSING, reason="Skills are optional and missing.")
    if len(items) > 30 or any(len(item) < 1 or len(item) > 50 for item in items):
        return FieldResult("skills", INVALID, reason="Provide up to 30 skills, each 1 to 50 characters.")
    return FieldResult("skills", VALID, value=items)


def _skill_key(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip().lower() for item in value]


def _parse_date(text: str) -> date | None:
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text.strip(), flags=re.IGNORECASE)
    iso = re.match(r"(\d{4}-\d{2}-\d{2})", cleaned)
    if iso:
        try:
            return date.fromisoformat(iso.group(1))
        except ValueError:
            return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", cleaned)
    if not slash:
        return None
    first, second, year = (int(part) for part in slash.groups())
    try:
        if first > 12 and second <= 12:
            return date(year, second, first)
        if second > 12 and first <= 12:
            return date(year, first, second)
    except ValueError:
        return None
    return None
