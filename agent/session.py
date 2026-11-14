"""Conversation state and the rules for what may be written to the database.

Python, not the model, owns validation, status changes, and PATCH bodies.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from services.audit_log import AuditLog
from services.candidate_api import CandidateAPI, CandidateAPIError
from services.validation import (
    INVALID,
    MANDATORY_FIELDS,
    MISSING,
    NEEDS_CLARIFICATION,
    OPTIONAL_FIELDS,
    PROFILE_FIELDS,
    VALID,
    FieldResult,
    all_mandatory_valid,
    assess_record,
    judge_extraction,
    outstanding,
    progress_count,
    same_value,
)

TERMINAL_STATUSES = {"completed", "in_progress", "no_answer", "refused", "needs_human"}
ALREADY_COMPLETED_MESSAGE = "Application already completed. No conversation needed."


class ExtractionItem(BaseModel):
    field: str
    value: str = ""
    signal: str = "clear"
    replaces_existing: bool = False
    quote: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def value_as_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return str(value)

    @field_validator("signal", mode="before")
    @classmethod
    def known_signal(cls, value: Any) -> str:
        text = str(value or "clear")
        if text not in {"clear", "ambiguous", "contradicts_previous"}:
            return "clear"
        return text


class EndRequest(BaseModel):
    outcome: str
    note: str = ""
    refused_fields: list[str] = Field(default_factory=list)

    @field_validator("refused_fields", mode="before")
    @classmethod
    def fields_as_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]


class Session:
    def __init__(self, api: CandidateAPI, record: dict[str, Any], audit: AuditLog) -> None:
        self.api = api
        self.audit = audit
        self.record = dict(record)
        self.candidate_id = str(record.get("id", "")).strip()
        self.version = record.get("version")
        self.assessments = assess_record(record)
        self.identity_confirmed = False
        self.profile_saved = False
        self.attempt_sent = False
        self.clarification_counts: dict[str, int] = {}
        self.refused_fields: list[str] = []
        self.stop = False
        self.outcome: str | None = None
        self.closing_line = ""
        self.original_notes = str(record.get("notes") or "").strip()
        self.attempt_count = _attempt(record.get("attempt_count"))
        self.last_candidate_text = ""
        self.evidence: list[dict[str, str]] = []
        self.saved_fields: list[str] = []
        self.last_save = ""

    def all_valid(self) -> bool:
        return all_mandatory_valid(self.assessments)

    def api_status(self) -> str:
        """Status stored by Prodigal. Field validity does not change this value."""
        return str(self.record.get("collection_status") or "")

    def begin(self) -> dict[str, Any]:
        """Open a chat only for an unfinished pending or in-progress record.

        A pending record whose mandatory fields are already valid is completed
        through the normal save and fresh GET. No other stored status is replaced.
        """
        status = self.api_status()
        if status == "completed":
            self.outcome = "completed"
            self.stop = True
            self.closing_line = ALREADY_COMPLETED_MESSAGE
            self.audit.event("decision", {"action": "skip_conversation", "reason": "api status completed"})
            self.audit.finish("completed")
            return {"ok": True, "chat": False, "updated": False, "outcome": "completed"}
        if status == "pending" and self.all_valid():
            result = self.mark_already_complete()
            result["chat"] = False
            return result
        if status in {"pending", "in_progress"} and not self.all_valid():
            return {"ok": True, "chat": True, "updated": False}
        self.stop = True
        shown = status or "missing"
        if self.all_valid():
            self.closing_line = (
                f"Collection status is {shown}. Every mandatory field is valid. "
                "The stored status was not changed, and no conversation was started."
            )
        else:
            self.closing_line = (
                f"Collection status is {shown}. Chat is closed, and the stored status was not changed."
            )
        self.audit.event(
            "decision",
            {"action": "skip_conversation", "reason": "status is not open for chat", "collection_status": shown},
        )
        self.audit.finish(None)
        return {"ok": True, "chat": False, "updated": False}

    def progress(self) -> tuple[int, int]:
        return progress_count(self.assessments)

    def snapshot(self) -> dict[str, Any]:
        """Progress for the terminal and the local site. Phone number is not included."""
        done, total = self.progress()
        fields = []
        for field in MANDATORY_FIELDS:
            result = self.assessments[field]
            fields.append(
                {
                    "field": field,
                    "status": result.status,
                    "read_only": field == "phone",
                    "reason": "" if field == "phone" else (result.reason or ""),
                }
            )
        return {
            "candidate_id": self.candidate_id,
            "name": self.display_name(),
            "done": done,
            "total": total,
            "complete": self.all_valid(),
            "outcome": self.outcome,
            "stopped": self.stop,
            "identity_confirmed": self.identity_confirmed,
            "collection_status": self.api_status(),
            "chat_enabled": not self.stop,
            "start_enabled": self.api_status() != "completed",
            "action_label": "Resume" if self.api_status() == "in_progress" and not self.stop else "Start",
            "disabled_reason": "" if not self.stop else (self.closing_line or "Chat is closed."),
            "outstanding": [item.field for item in outstanding(self.assessments)],
            "fields": fields,
            "evidence": list(self.evidence),
            "saved_fields": list(self.saved_fields),
            "last_save": self.last_save,
        }

    def display_name(self) -> str | None:
        name = self.assessments["full_name"]
        if name.status == VALID and isinstance(name.value, str):
            return name.value
        return None

    def greeting(self) -> str:
        name = self.display_name()
        if name:
            return (
                f"Hello {name}. This is Completr AI. I am here to help you finish "
                f"your ACME Corp internship application. Am I speaking with {name}?"
            )
        return (
            "Hello. This is Completr AI. I am here to help you finish your ACME Corp "
            "internship application. Please tell me your full name so I know who I am speaking with."
        )

    def state_block(self) -> str:
        done, total = self.progress()
        lines = [
            f"Identity confirmed: {'yes' if self.identity_confirmed else 'no'}",
            f"Progress: {done}/{total} mandatory fields valid",
            "Fields:",
        ]
        for field in MANDATORY_FIELDS + OPTIONAL_FIELDS:
            result = self.assessments[field]
            if field == "phone":
                lines.append("- phone: VALID | read-only | do not ask for it and do not update it")
                continue
            optional = " | optional" if field in OPTIONAL_FIELDS else ""
            detail = f"- {field}: {result.status}{optional}"
            if result.status == VALID:
                detail += f" | stored: {_short(result.value)}"
            elif result.reason and result.status in {INVALID, NEEDS_CLARIFICATION}:
                detail += f" | {result.reason}"
            repeats = self.clarification_counts.get(field, 0)
            if repeats:
                detail += f" | clarification attempts: {repeats}"
            lines.append(detail)
        remaining = [item.field for item in outstanding(self.assessments)]
        lines.append("Still required: " + (", ".join(remaining) if remaining else "none"))
        return "\n".join(lines)

    def dispatch(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        args = args or {}
        self.audit.event("decision", {"tool": name, "args": args})
        if name == "get_application_state":
            return {"state": self.state_block()}
        if name == "confirm_identity":
            return self._confirm_identity(bool(args.get("confirmed")), str(args.get("note") or ""))
        if name == "record_candidate_fields":
            return self._record_fields(args)
        if name == "end_conversation":
            try:
                request = EndRequest.model_validate(args)
            except Exception as exc:  # noqa: BLE001 - return the failure to the model
                return {"ok": False, "error": f"end_conversation arguments were not usable: {exc}"}
            return self.finish(request.outcome, request.note, request.refused_fields)
        return {"ok": False, "error": f"Unknown tool {name}."}

    def outcome_for_departure(self, note: str) -> dict[str, Any]:
        """Choose a status when the person leaves with /quit or a closed terminal."""
        if self.all_valid():
            outcome = "completed"
        elif self.profile_saved:
            outcome = "in_progress"
        else:
            outcome = "no_answer"
        return self.finish(outcome, note, self.refused_fields)

    def finish(self, outcome: str, note: str, refused_fields: list[str]) -> dict[str, Any]:
        if self.stop and self.outcome:
            return {"ok": True, "outcome": self.outcome, "message": "The conversation is already closed."}

        refused = [field for field in refused_fields if field in MANDATORY_FIELDS and field != "phone"]
        optional_refused = [field for field in refused_fields if field in OPTIONAL_FIELDS]
        actual = outcome if outcome in TERMINAL_STATUSES else "needs_human"

        if actual == "completed" and not self.all_valid():
            remaining = [item.field for item in outstanding(self.assessments)]
            return {
                "ok": False,
                "error": "Cannot complete yet.",
                "remaining_fields": remaining,
                "instruction": "Ask only about the remaining mandatory fields.",
            }
        if actual == "refused" and not refused:
            if optional_refused:
                return {
                    "ok": False,
                    "error": "Refusing an optional field does not stop completion.",
                    "optional_fields": optional_refused,
                    "instruction": "Continue with the mandatory fields that are still open.",
                }
            return {
                "ok": False,
                "error": "Name the mandatory fields the candidate refused.",
            }
        if actual == "completed":
            refused = []

        self.refused_fields = refused
        self.outcome = actual
        notes = self._notes(actual, note, refused)
        tracking: dict[str, Any] = {
            "collection_status": actual,
            "notes": notes,
            "last_contacted_at": _timestamp(),
        }
        saved = self._save(tracking, include_attempt=True)
        fresh, mismatches = self._verify(saved.get("sent") or {})
        self.stop = True
        self.closing_line = _closing(actual, refused, saved.get("error"))
        self.audit.event(
            "outcome",
            {
                "outcome": actual,
                "note": note,
                "refused_fields": refused,
                "optional_refusals_ignored": optional_refused,
                "verify_mismatches": mismatches,
                "version": self.version,
            },
        )
        self.audit.finish(actual)
        result = {
            "ok": saved.get("ok", False),
            "outcome": actual,
            "saved_fields": sorted((saved.get("sent") or {}).keys()),
            "verify_mismatches": mismatches,
        }
        if saved.get("error"):
            result["error"] = saved["error"]
        if fresh is None:
            result["verified"] = False
        else:
            result["verified"] = not mismatches
        return result

    def mark_already_complete(self) -> dict[str, Any]:
        """Complete a pending record whose mandatory fields are already valid.

        The write uses the normal PATCH and a fresh GET. A record that is already
        completed is left unchanged. Any other stored status is also left unchanged.
        """
        status = self.api_status()
        if status == "completed":
            self.outcome = "completed"
            self.stop = True
            self.closing_line = ALREADY_COMPLETED_MESSAGE
            self.audit.event("decision", {"action": "skip_conversation", "reason": "api status completed"})
            self.audit.finish("completed")
            return {"ok": True, "updated": False, "outcome": "completed", "chat": False}
        if status != "pending" or not self.all_valid():
            self.stop = True
            shown = status or "missing"
            self.closing_line = f"Collection status is {shown}. It was not changed."
            return {"ok": False, "updated": False, "chat": False}

        tracking = {
            "collection_status": "completed",
            "notes": self._notes(
                "completed",
                "All mandatory fields were already valid. No conversation was started.",
                [],
            ),
        }
        saved = self._save(tracking, include_attempt=False)
        fresh, mismatches = self._verify(saved.get("sent") or {})
        verified = (
            bool(saved.get("ok"))
            and isinstance(fresh, dict)
            and str(fresh.get("collection_status") or "") == "completed"
            and not mismatches
        )
        self.stop = True
        if verified:
            self.outcome = "completed"
            self.closing_line = (
                "All mandatory fields are valid and the stored status was pending. "
                "The application is now completed. No conversation is needed."
            )
        else:
            self.outcome = None
            self.closing_line = "The completion update could not be verified. No conversation was started."
            if saved.get("error"):
                self.closing_line += " " + str(saved["error"])
        self.audit.event(
            "outcome",
            {
                "outcome": "completed" if verified else None,
                "skipped_conversation": True,
                "verify_mismatches": mismatches,
                "verified": verified,
            },
        )
        self.audit.finish("completed" if verified else None)
        return {
            "ok": verified,
            "updated": bool(saved.get("ok")),
            "outcome": "completed" if verified else None,
            "verify_mismatches": mismatches,
            "fresh": fresh is not None,
            "chat": False,
        }

    def _confirm_identity(self, confirmed: bool, note: str) -> dict[str, Any]:
        if confirmed:
            self.identity_confirmed = True
            self.audit.event("decision", {"identity_confirmed": True, "note": note})
            return {
                "ok": True,
                "identity_confirmed": True,
                "instruction": "Collect the fields that are not VALID. Do not ask for phone or for fields that are already VALID.",
                "state": self.state_block(),
            }
        self.audit.event("decision", {"identity_confirmed": False, "note": note})
        return self.finish("needs_human", note or "Candidate said this is the wrong person.", [])

    def _record_fields(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.stop:
            return {
                "ok": False,
                "error": "The conversation is already closed.",
                "outcome": self.outcome,
            }
        if not self.identity_confirmed:
            return {
                "ok": False,
                "error": "Identity is not confirmed yet. Call confirm_identity before saving fields.",
            }
        raw_fields = args.get("fields", [])
        if isinstance(raw_fields, dict):
            raw_fields = [raw_fields]
        if not isinstance(raw_fields, list):
            return {"ok": False, "error": "fields must be a list."}

        results = []
        accepted: dict[str, Any] = {}
        escalate = False
        parsed: list[ExtractionItem] = []
        for raw in raw_fields:
            try:
                item = ExtractionItem.model_validate(raw)
            except Exception as exc:  # noqa: BLE001
                results.append({"ok": False, "error": str(exc)})
                continue
            item.field = _canonical_field(item.field)
            parsed.append(item)
        parsed = _split_degree_and_branch(parsed)
        for item in parsed:
            current = self.assessments.get(item.field) or FieldResult(item.field, MISSING)
            judged = judge_extraction(
                item.field,
                item.value,
                item.signal,
                item.replaces_existing,
                current,
            )
            quote = _supporting_quote(self.last_candidate_text, item.value, item.quote)
            if judged.status == NEEDS_CLARIFICATION:
                decision = "clarification"
            elif judged.status == VALID and not (
                current.status == VALID and same_value(item.field, current.value, judged.value)
            ):
                decision = "save"
            else:
                decision = "reject"
            self.audit.event(
                "evidence",
                {
                    "field": item.field,
                    "value": item.value,
                    "quote": quote,
                    "validation": judged.status,
                    "decision": decision,
                    "normalized": judged.value if judged.status == VALID else None,
                    "reason": judged.reason,
                    "signal": item.signal,
                },
            )
            if item.field != "phone":
                self.evidence.append(
                    {
                        "field": item.field,
                        "quote": quote,
                        "status": judged.status,
                        "decision": decision,
                    }
                )
            entry: dict[str, Any] = {
                "field": item.field,
                "status": judged.status,
                "reason": judged.reason,
            }
            if judged.status == VALID:
                if current.status == VALID and same_value(item.field, current.value, judged.value):
                    entry["stored"] = False
                    entry["reason"] = "Already valid. Left unchanged."
                else:
                    self.assessments[item.field] = judged
                    accepted[item.field] = judged.value
                    entry["stored"] = True
                    entry["normalized"] = judged.value
                self.clarification_counts.pop(item.field, None)
            elif judged.status == NEEDS_CLARIFICATION:
                self.clarification_counts[item.field] = self.clarification_counts.get(item.field, 0) + 1
                entry["stored"] = False
                if self.clarification_counts[item.field] >= 2:
                    escalate = True
                    entry["escalate"] = True
            else:
                entry["stored"] = False
            results.append(entry)

        save_result: dict[str, Any] | None = None
        if accepted:
            self.profile_saved = True
            tracking = {
                "collection_status": "in_progress",
                "notes": self._notes("in_progress", "Saved valid answers during the conversation.", []),
                "last_contacted_at": _timestamp(),
                **accepted,
            }
            save_result = self._save(tracking, include_attempt=True)

        if escalate:
            fields = [entry["field"] for entry in results if entry.get("escalate")]
            self.finish(
                "needs_human",
                "Could not resolve conflicting or ambiguous answers for: " + ", ".join(fields),
                [],
            )
            return {
                "ok": False,
                "results": results,
                "escalated": True,
                "outcome": "needs_human",
                "instruction": "Tell the candidate a person will follow up. Do not keep asking.",
            }

        done, total = self.progress()
        payload = {
            "ok": True,
            "results": results,
            "progress": f"{done}/{total}",
            "all_mandatory_valid": self.all_valid(),
            "saved": save_result.get("ok") if save_result else False,
        }
        if save_result and save_result.get("ok"):
            for name in accepted:
                if name not in self.saved_fields:
                    self.saved_fields.append(name)
            self.last_save = "Saved " + ", ".join(accepted) + "."
        elif save_result and save_result.get("error"):
            self.last_save = "The save did not succeed."
            payload["save_error"] = save_result["error"]
        if self.all_valid():
            payload["instruction"] = (
                "Every mandatory field is valid. Thank the candidate and call "
                "end_conversation with outcome completed. Do not ask more mandatory questions."
            )
        else:
            remaining = [item.field for item in outstanding(self.assessments)]
            payload["instruction"] = "Ask only about: " + ", ".join(remaining)
        return payload

    def _notes(self, outcome: str, note: str, refused: list[str]) -> str:
        remaining = [item.field for item in outstanding(self.assessments)]
        parts = [f"Completr AI outcome: {outcome}."]
        if remaining:
            parts.append("Remaining: " + ", ".join(remaining) + ".")
        else:
            parts.append("All mandatory fields are valid.")
        if refused:
            parts.append("Refused: " + ", ".join(refused) + ".")
        if note.strip():
            parts.append(note.strip())
        if self.original_notes:
            parts.append("Earlier notes: " + self.original_notes)
        return " ".join(parts)[:4000]

    def _save(self, fields: dict[str, Any], include_attempt: bool) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in fields.items()
            if key not in {"id", "phone", "version", "updated_at"} and value is not None
        }
        if include_attempt and not self.attempt_sent:
            payload["attempt_count"] = min(50, self.attempt_count + 1)
        if not payload:
            return {"ok": True, "sent": {}, "skipped": True}

        idempotency_key = str(uuid.uuid4())
        self.audit.event(
            "database_update",
            {
                "method": "PATCH",
                "path": f"/v1/candidates/{self.candidate_id}",
                "if_match": self.version,
                "idempotency_key": idempotency_key,
                "body": payload,
            },
        )
        try:
            response = self._patch_with_conflict_retry(payload, idempotency_key)
        except CandidateAPIError as exc:
            self.audit.event("api_response", {"ok": False, "status_code": exc.status_code, "error": str(exc)})
            return {"ok": False, "sent": payload, "error": str(exc)}

        self._absorb(response, payload)
        if "attempt_count" in payload:
            self.attempt_sent = True
            self.attempt_count = int(payload["attempt_count"])
        self.audit.event("api_response", {"ok": True, "method": "PATCH", "body": response})
        return {"ok": True, "sent": payload, "response": response}

    def _patch_with_conflict_retry(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        try:
            return self.api.update_candidate(self.candidate_id, payload, self.version, idempotency_key)
        except CandidateAPIError as exc:
            if exc.status_code != 409:
                raise
            latest = self.api.get_candidate(self.candidate_id)
            self.audit.event("api_response", {"method": "GET", "reason": "conflict_reread", "body": latest})
            if isinstance(latest, dict):
                self._absorb(latest, None)
            reconciled = {}
            for key, value in payload.items():
                current = self.record.get(key)
                if key in PROFILE_FIELDS and same_value(key, current, value):
                    continue
                reconciled[key] = value
            if not reconciled:
                return latest
            retry_key = str(uuid.uuid4())
            self.audit.event(
                "database_update",
                {
                    "method": "PATCH",
                    "reason": "reconcile_after_409",
                    "if_match": self.version,
                    "idempotency_key": retry_key,
                    "body": reconciled,
                },
            )
            return self.api.update_candidate(self.candidate_id, reconciled, self.version, retry_key)

    def _verify(self, sent: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            fresh = self.api.get_candidate(self.candidate_id)
        except CandidateAPIError as exc:
            self.audit.event("api_response", {"method": "GET", "reason": "verify", "ok": False, "error": str(exc)})
            return None, ["verify_get_failed"]
        self.audit.event("api_response", {"method": "GET", "reason": "verify", "body": fresh})
        if not isinstance(fresh, dict):
            return None, ["verify_get_not_object"]
        self._absorb(fresh, None)
        mismatches = []
        for key, value in sent.items():
            if key in {"notes", "last_contacted_at", "idempotency_key"}:
                continue
            current = fresh.get(key)
            if key in PROFILE_FIELDS:
                if not same_value(key, current, value):
                    mismatches.append(key)
            elif current != value:
                mismatches.append(key)
        return fresh, mismatches


    def _absorb(self, body: Any, fallback: dict[str, Any] | None) -> None:
        """Take a new version from the API without dropping fields the body left out."""
        if isinstance(body, dict) and body.get("id"):
            self.record = dict(body)
            self.version = body.get("version", self.version)
            self.assessments = assess_record(self.record)
            return
        if isinstance(body, dict) and body.get("version") is not None:
            self.version = body["version"]
        if not fallback:
            return
        for key, value in fallback.items():
            self.record[key] = value

def _canonical_field(name: str) -> str:
    key = " ".join(name.replace("-", " ").replace("_", " ").strip().lower().split())
    aliases = {
        "full name": "full_name",
        "name": "full_name",
        "email": "email",
        "email address": "email",
        "college": "college",
        "institution": "college",
        "university": "college",
        "school": "college",
        "degree": "degree",
        "branch": "branch",
        "major": "branch",
        "specialisation": "branch",
        "specialization": "branch",
        "stream": "branch",
        "graduation year": "graduation_year",
        "year": "graduation_year",
        "cgpa": "cgpa",
        "gpa": "cgpa",
        "preferred location": "preferred_location",
        "location": "preferred_location",
        "city": "preferred_location",
        "available from": "available_from",
        "start date": "available_from",
        "availability": "available_from",
        "internship duration": "internship_duration_months",
        "internship duration months": "internship_duration_months",
        "duration": "internship_duration_months",
        "linkedin": "linkedin_url",
        "linkedin url": "linkedin_url",
        "github": "github_url",
        "github url": "github_url",
        "skills": "skills",
    }
    return aliases.get(key, name.strip())


def _split_degree_and_branch(items: list[ExtractionItem]) -> list[ExtractionItem]:
    """If the model packs branch into the degree, separate the two fields."""
    present = {item.field for item in items}
    if "branch" in present:
        return items
    extra: list[ExtractionItem] = []
    for item in items:
        if item.field != "degree" or not re.search(r"\sin\s", item.value, flags=re.IGNORECASE):
            continue
        left, right = re.split(r"\s+in\s+", item.value, maxsplit=1, flags=re.IGNORECASE)
        if left.strip() and right.strip():
            item.value = left.strip()
            extra.append(
                ExtractionItem(
                    field="branch",
                    value=right.strip(),
                    signal=item.signal,
                    replaces_existing=item.replaces_existing,
                    quote=item.quote,
                )
            )
    return items + extra


def _supporting_quote(message: str, value: str, provided: str) -> str:
    """Use a quote only when it appears in the candidate's own message."""
    text = message or ""
    supplied = (provided or "").strip()
    if supplied and supplied.lower() in text.lower():
        return supplied[:300]
    needle = str(value or "").strip()
    if not text or not needle:
        return ""
    index = text.lower().find(needle.lower())
    if index < 0:
        return ""
    start = max(text.rfind(".", 0, index), text.rfind("!", 0, index), text.rfind("?", 0, index))
    start = 0 if start < 0 else start + 1
    end = len(text)
    for mark in (".", "!", "?"):
        found = text.find(mark, index + len(needle))
        if found >= 0:
            end = min(end, found + 1)
    return " ".join(text[start:end].split())[:300]


def _attempt(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short(value: Any) -> str:
    text = ", ".join(value) if isinstance(value, list) else str(value)
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _closing(outcome: str, refused: list[str], error: str | None) -> str:
    if error:
        return "I could not save the latest update. A person will need to check the application."
    if outcome == "completed":
        return "Thank you. Your internship application is complete, and the details are saved."
    if outcome == "in_progress":
        return "No problem. I saved the answers we already confirmed. We can continue another time."
    if outcome == "no_answer":
        return "I have noted that we did not collect the application details this time."
    if outcome == "refused":
        fields = ", ".join(field.replace("_", " ") for field in refused) or "a required field"
        return f"I understand. I have recorded that you do not want to share {fields}."
    return "I will stop here and ask a person on the team to follow up."
