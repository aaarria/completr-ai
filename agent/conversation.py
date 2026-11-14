"""Gemini function-calling loop for one candidate.

The model decides what to ask and which tools to call. Python validates
every extracted value and is the only code that writes to the database.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logging.getLogger("google.genai").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)

from google import genai
from google.genai import types

from agent.session import Session
from services.audit_log import AuditLog
from services.candidate_api import CandidateAPI, CandidateAPIError, load_project_env

_TOOL_ORDER = {
    "confirm_identity": 0,
    "record_candidate_fields": 1,
    "get_application_state": 2,
    "end_conversation": 3,
}

ACTIVE_MODEL = "gemini-3.5-flash-lite"
_PREFERRED_MODELS = (
    ACTIVE_MODEL,
    "gemini-3.1-flash-lite",
)


class GeminiError(Exception):
    """A model call failed. The message has already had secrets removed."""


def run_agent(candidate_id: str) -> int:
    load_project_env()
    api = CandidateAPI()
    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    secrets = [api._api_key, gemini_key]
    audit = AuditLog(candidate_id, secrets)

    if not api.has_api_key():
        print("PRODIGAL_API_KEY is missing. Add it to .env and run this again.", flush=True)
        audit.event("outcome", {"error": "missing prodigal api key"})
        audit.finish(None)
        api.close()
        return 1
    if not gemini_key:
        print("GEMINI_API_KEY is missing. Add it to .env and run this again.", flush=True)
        audit.event("outcome", {"error": "missing gemini api key"})
        audit.finish(None)
        api.close()
        return 1

    try:
        record = api.get_candidate(candidate_id)
        schema = api.get_schema()
    except CandidateAPIError as exc:
        print(f"Could not load this candidate: {exc}", flush=True)
        audit.event("api_response", {"ok": False, "error": str(exc), "status_code": exc.status_code})
        audit.finish(None)
        api.close()
        return 1

    if not isinstance(record, dict) or not record.get("id"):
        print("The candidate response did not include a record id.", flush=True)
        audit.event("api_response", {"ok": False, "error": "missing id", "body": record})
        audit.finish(None)
        api.close()
        return 1

    audit.event("api_response", {"method": "GET", "path": f"/v1/candidates/{candidate_id}", "body": record})
    audit.event("api_response", {"method": "GET", "path": "/v1/schema", "body": schema})
    audit.event(
        "decision",
        {
            "schema_mandatory_fields": schema.get("mandatory_fields") if isinstance(schema, dict) else None,
            "schema_statuses": schema.get("collection_statuses") if isinstance(schema, dict) else None,
        },
    )

    session = Session(api, record, audit)
    _print_banner(session)

    decision = session.begin()
    if not decision.get("chat"):
        print(session.closing_line, flush=True)
        audit.say("agent", session.closing_line)
        api.close()
        return 0 if decision.get("ok") else 1

    greeting = session.greeting()
    print(f"Completr: {greeting}", flush=True)
    audit.say("agent", greeting)

    try:
        client = GeminiClient(gemini_key, session, audit)
    except GeminiError as exc:
        print(f"Completr: {exc}", flush=True)
        audit.finish(None)
        api.close()
        return 1

    exit_code = 0
    try:
        while not session.stop:
            try:
                user_text = input("You: ").strip()
            except EOFError:
                audit.say("candidate", "")
                result = session.outcome_for_departure("Candidate left before the chat finished.")
                print(f"Completr: {session.closing_line}", flush=True)
                audit.say("agent", session.closing_line)
                exit_code = 0 if result.get("ok", True) else 1
                break

            if not user_text:
                print("Completr: I did not catch that. Type a reply, or /quit to stop.", flush=True)
                continue
            audit.say("candidate", user_text)
            if user_text.lower() == "/quit":
                result = session.outcome_for_departure("Candidate typed /quit.")
                print(f"Completr: {session.closing_line}", flush=True)
                audit.say("agent", session.closing_line)
                exit_code = 0 if result.get("ok", True) else 1
                break
            if not session.identity_confirmed:
                identity = _identity_answer(user_text)
                if identity == "no":
                    result = session.finish(
                        "needs_human",
                        "Candidate said this is the wrong person.",
                        [],
                    )
                    print(f"Completr: {session.closing_line}", flush=True)
                    audit.say("agent", session.closing_line)
                    exit_code = 0 if result.get("ok") else 1
                    break
                if identity == "yes":
                    session.identity_confirmed = True
                    audit.event("decision", {"identity_confirmed": True, "source": "explicit reply"})

            try:
                reply, stopped = client.handle_user_turn(user_text)
            except GeminiError as exc:
                print(f"Completr: I could not reach the assistant just now. {exc}", flush=True)
                audit.event("decision", {"gemini_error": str(exc)})
                exit_code = 1
                break
            except CandidateAPIError as exc:
                print(f"Completr: I could not update the application. {exc}", flush=True)
                exit_code = 1
                break

            if reply:
                print(f"Completr: {reply}", flush=True)
                audit.say("agent", reply)
            _print_progress(session)
            if stopped:
                if session.closing_line and session.closing_line not in reply:
                    print(f"Completr: {session.closing_line}", flush=True)
                    audit.say("agent", session.closing_line)
                break
    finally:
        if session.outcome is None:
            audit.finish(None)
        api.close()
    return exit_code


class GeminiClient:
    def __init__(self, api_key: str, session: Session, audit: AuditLog) -> None:
        self.session = session
        self.audit = audit
        self.client = genai.Client(api_key=api_key)
        self.models = [ACTIVE_MODEL, "gemini-3.1-flash-lite"]
        self.model = ACTIVE_MODEL
        self.contents: list[types.Content] = []
        self.tool = types.Tool(function_declarations=_declarations())
        self.audit.event("decision", {"model": self.model})

    def handle_user_turn(self, user_text: str) -> tuple[str, bool]:
        self.session.last_candidate_text = user_text
        self.contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        spoken: list[str] = []
        for _ in range(6):
            if self.session.stop:
                break
            response = self._generate()
            content = _model_content(response)
            calls = list(response.function_calls or [])
            text = (response.text or "").strip()
            if calls:
                if content is not None:
                    self.contents.append(content)
                parts = []
                for call in sorted(calls, key=lambda item: _TOOL_ORDER.get(item.name or "", 9)):
                    result = self.session.dispatch(call.name or "", dict(call.args or {}))
                    self.audit.event(
                        "tool_call",
                        {"name": call.name, "args": dict(call.args or {}), "result": result},
                    )
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=call.name,
                                id=call.id,
                                response={"result": result},
                            )
                        )
                    )
                self.contents.append(types.Content(role="user", parts=parts))
                if text:
                    spoken.append(text)
                if self.session.stop:
                    break
                continue
            if content is not None:
                self.contents.append(content)
            if text:
                spoken.append(text)
                break
            self.contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part(text="Reply to the candidate in plain text, or call the right tool.")],
                )
            )
        unique: list[str] = []
        for line in spoken:
            if line not in unique:
                unique.append(line)
        reply = "\n\n".join(unique)
        if self.session.stop and not reply:
            reply = self.session.closing_line
        return reply, self.session.stop

    def _generate(self) -> Any:
        last_error = "Gemini request failed."
        start = self.models.index(self.model) if self.model in self.models else 0
        for model in self.models[start:]:
            try:
                response = self._generate_with_model(model)
            except GeminiError as exc:
                last_error = str(exc)
                if "invalid_argument" in last_error.lower() or last_error.startswith("Gemini rejected"):
                    self.audit.event("decision", {"invalid_argument": model, "error": last_error})
                    raise
                if _quota_or_missing(last_error) and model != self.models[-1]:
                    self.audit.event("decision", {"model_unavailable": model, "error": last_error})
                    continue
                raise
            self.model = model
            return response
        raise GeminiError(last_error)

    def _generate_with_model(self, model: str) -> Any:
        config = types.GenerateContentConfig(
            system_instruction=_system_prompt(self.session),
            tools=[self.tool],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            ),
            temperature=0.4,
            max_output_tokens=1024,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            return self.client.models.generate_content(
                model=model,
                contents=self.contents,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - the SDK raises several error types
            message = _public_gemini_error(_redact(str(exc), self.audit._secrets))
            raise GeminiError(message) from exc


def _system_prompt(session: Session) -> str:
    return f"""You are Completr AI, helping one ACME Corp internship candidate finish an application.

You decide the next step. Use the tools. Do not invent facts, ids, phone numbers, or values the candidate did not say.

Identity:
- Greetings already happened. The candidate's next messages are their replies.
- If they confirm they are the named person, call confirm_identity with confirmed true before saving any field.
- If they say you have the wrong person, call confirm_identity with confirmed false.
- Never ask for a candidate id or a phone number.

Collecting information:
- When a message contains application facts, call record_candidate_fields before you reply.
- Extract every field in that message. One sentence can hold degree, branch, college, graduation year, and CGPA together.
- For each field, set quote to the exact candidate words that support it. Do not invent a quote.
- signal=clear only when the value is explicit.
- signal=ambiguous for ranges, guesses, or answers that could mean more than one value. Do not pick one for them.
- signal=contradicts_previous when they disagree with a stored valid value. Do not replace it unless they clearly confirm the new value, and then set replaces_existing true.
- Never say a value was saved unless the tool accepted it.
- Do not ask about a field whose status is VALID.
- Phone is read-only.
- linkedin_url, github_url, and skills are optional. A refusal of an optional field must not end the application.
- If they refuse a mandatory field, call end_conversation with outcome refused and list those fields.
- If they want to continue later, call end_conversation with outcome in_progress and put their timing in the note.
- If they leave without giving application information, call end_conversation with outcome no_answer.
- If they are upset, or a contradiction stays unresolved, call end_conversation with outcome needs_human.
- When the tool says all mandatory fields are valid, thank them and call end_conversation with outcome completed.

Keep each reply to two or three short sentences.

Current application state:
{session.state_block()}
"""


def _declarations() -> list[types.FunctionDeclaration]:
    field_enum = [
        "full_name",
        "email",
        "college",
        "degree",
        "branch",
        "graduation_year",
        "cgpa",
        "preferred_location",
        "available_from",
        "internship_duration_months",
        "linkedin_url",
        "github_url",
        "skills",
    ]
    return [
        types.FunctionDeclaration(
            name="get_application_state",
            description="Read which fields are valid, missing, invalid, or waiting on clarification.",
        ),
        types.FunctionDeclaration(
            name="confirm_identity",
            description="Record whether the person confirmed they are the candidate you greeted.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "confirmed": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["confirmed"],
            },
        ),
        types.FunctionDeclaration(
            name="record_candidate_fields",
            description=(
                "Validate and store every application field found in the latest message. "
                "Call this before replying when the candidate provided any facts."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "enum": field_enum},
                                "value": {"type": "string"},
                                "signal": {
                                    "type": "string",
                                    "enum": ["clear", "ambiguous", "contradicts_previous"],
                                },
                                "replaces_existing": {"type": "boolean"},
                                "quote": {
                                    "type": "string",
                                    "description": "Exact words from the candidate that support this field. Copy them. Do not paraphrase.",
                                },
                            },
                            "required": ["field", "value", "signal"],
                        },
                    }
                },
                "required": ["fields"],
            },
        ),
        types.FunctionDeclaration(
            name="end_conversation",
            description="Stop the chat and save the outcome. Python rejects completed when required fields are still open.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": ["completed", "in_progress", "no_answer", "refused", "needs_human"],
                    },
                    "note": {"type": "string"},
                    "refused_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["outcome", "note"],
            },
        ),
    ]


def _model_order(client: genai.Client) -> list[str]:
    override = (os.getenv("GEMINI_MODEL") or "").strip()
    names = [override] if override else []
    names.extend(_PREFERRED_MODELS)
    available = set()
    try:
        for model in client.models.list():
            name = str(getattr(model, "name", "")).removeprefix("models/")
            if name:
                available.add(name)
    except Exception:
        available = set()
    ordered: list[str] = []
    for name in names:
        if not name or name in ordered:
            continue
        if available and name not in available and name != override:
            continue
        ordered.append(name)
    return ordered or [_PREFERRED_MODELS[0]]


def _choose_model(client: genai.Client) -> str:
    names: list[str] = []
    try:
        for model in client.models.list():
            name = str(getattr(model, "name", "")).removeprefix("models/")
            if name:
                names.append(name)
    except Exception:
        return _PREFERRED_MODELS[0]
    for preferred in _PREFERRED_MODELS:
        if preferred in names:
            return preferred
    for name in names:
        lowered = name.lower()
        if "flash" in lowered and "embed" not in lowered and "image" not in lowered:
            return name
    return _PREFERRED_MODELS[0]


def _model_content(response: Any) -> Any:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    return getattr(candidates[0], "content", None)


def _print_banner(session: Session) -> None:
    done, total = session.progress()
    name = session.display_name() or "name not confirmed yet"
    print("COMPLETR AI", flush=True)
    print("Intelligent Application Completion", flush=True)
    print(flush=True)
    print(f"Candidate: {name}", flush=True)
    print(f"Progress: {done}/{total} mandatory fields valid", flush=True)
    if not session.all_valid():
        print("Type /quit to end this session.", flush=True)
    print(flush=True)


def _print_progress(session: Session) -> None:
    done, total = session.progress()
    print(f"Progress: {done}/{total} mandatory fields valid", flush=True)


def _identity_answer(text: str) -> str:
    """Classify an obvious yes or no. Anything else is left for the model."""
    lowered = " ".join(text.lower().strip().split())
    if "no preference" in lowered:
        return "unclear"
    denial = ("wrong person", "not me", "that's not me", "that is not me", "you have the wrong")
    if lowered in {"no", "no.", "nope", "nah"} or any(marker in lowered for marker in denial):
        return "no"
    first = lowered.split(" ", 1)[0].strip(",.")
    if first in {"yes", "yeah", "yep", "yup", "correct"}:
        return "yes"
    if any(phrase in lowered for phrase in ("this is me", "that's me", "that is me", "i am speaking")):
        return "yes"
    return "unclear"


def _quota_or_missing(message: str) -> bool:
    lowered = message.lower()
    return "quota" in lowered or "not available" in lowered or "resource_exhausted" in lowered


def _public_gemini_error(message: str) -> str:
    lowered = message.lower()
    if "resource_exhausted" in lowered or "quota" in lowered:
        return "Gemini quota is exhausted for this model."
    if "not_found" in lowered or "no longer available" in lowered:
        return "The selected Gemini model is not available."
    if "invalid_argument" in lowered:
        return "Gemini rejected the request: " + _api_error_text(message)
    head = message.split("{", 1)[0].strip()
    return head[:240] or "Gemini request failed."


def _api_error_text(message: str) -> str:
    """Keep the API's own error text so a 400 is visible instead of a bare status."""
    start = message.find("{")
    if start == -1:
        return message[:500]
    try:
        payload = json.loads(message[start:])
    except json.JSONDecodeError:
        return message[:500]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:500]
    return message[:500]


def _redact(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned
