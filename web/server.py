"""Local chat site that uses the same agent and Prodigal API as the terminal.

Run: python -m web.server
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from google.genai import types

from agent.conversation import GeminiClient, GeminiError, _identity_answer
from agent.session import Session
from services.audit_log import RUNS_DIR, AuditLog
from services.candidate_api import CandidateAPI, CandidateAPIError, _candidate_records, load_project_env
from services.validation import assess_record, progress_count

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "8000"))
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _use_direct_connection() -> None:
    """The CLI reaches Prodigal directly. A proxy in this process makes that same call fail."""
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)


def _public_api_error(exc: CandidateAPIError) -> str:
    """Show the underlying HTTP or connection error without including API keys."""
    text = str(exc)
    cause = exc.__cause__
    if cause is not None:
        cause_text = str(cause).split("\n", 1)[0][:300]
        if cause_text and cause_text not in text:
            text = f"{text} {cause_text}"
    for name in ("PRODIGAL_API_KEY", "GEMINI_API_KEY"):
        secret = os.getenv(name) or ""
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


class Office:
    """One local session. The evaluator uses a single browser tab."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.api: CandidateAPI | None = None
        self.session: Session | None = None
        self.client: GeminiClient | None = None
        self.audit: AuditLog | None = None
        self.messages: list[dict[str, str]] = []

    def start(self, candidate_id: str) -> dict:
        with self.lock:
            self.close()
            _use_direct_connection()
            load_project_env()
            api = CandidateAPI()
            gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
            if not api.has_api_key():
                api.close()
                return _error("PRODIGAL_API_KEY is missing from .env.")
            if not gemini_key:
                api.close()
                return _error("GEMINI_API_KEY is missing from .env.")
            audit = AuditLog(candidate_id, [api._api_key, gemini_key])
            try:
                record = api.get_candidate(candidate_id)
                schema = api.get_schema()
            except CandidateAPIError as exc:
                audit.event("api_response", {"ok": False, "error": str(exc)})
                audit.finish(None)
                api.close()
                return _error(_public_api_error(exc))
            if not isinstance(record, dict) or not record.get("id"):
                api.close()
                return _error("The candidate response did not include a record.")
            audit.event("api_response", {"method": "GET", "path": f"/v1/candidates/{candidate_id}", "body": record})
            audit.event("api_response", {"method": "GET", "path": "/v1/schema", "body": schema})
            session = Session(api, record, audit)
            self.api = api
            self.session = session
            self.audit = audit
            self.messages = []
            decision = session.begin()
            if not decision.get("chat"):
                if session.closing_line:
                    self._say("agent", session.closing_line)
                return self._payload()
            greeting = session.greeting()
            self._say("agent", greeting)
            self.client = GeminiClient(gemini_key, session, audit)
            self.client.contents.append(
                types.Content(role="model", parts=[types.Part(text=greeting)])
            )
            return self._payload()

    def message(self, text: str) -> dict:
        with self.lock:
            session = self.session
            audit = self.audit
            if session is None or audit is None:
                return _error("Start a candidate session first.")
            text = text.strip()
            if not text:
                return _error("Type a reply first.")
            if session.stop:
                return self._payload()
            self._say("candidate", text)
            session.last_candidate_text = text
            if text.lower() == "/quit":
                session.outcome_for_departure("Candidate typed /quit.")
                self._say("agent", session.closing_line)
                return self._payload()
            if not session.identity_confirmed:
                identity = _identity_answer(text)
                if identity == "no":
                    session.finish("needs_human", "Candidate said this is the wrong person.", [])
                    self._say("agent", session.closing_line)
                    return self._payload()
                if identity == "yes":
                    session.identity_confirmed = True
                    audit.event("decision", {"identity_confirmed": True, "source": "explicit reply"})
            if self.client is None:
                return self._payload()
            try:
                reply, stopped = self.client.handle_user_turn(text)
            except (GeminiError, CandidateAPIError) as exc:
                notice = _public_turn_error(exc)
                audit.event("decision", {"error": notice})
                self._say("agent", notice)
                payload = self._payload()
                payload["notice"] = notice
                return payload
            except Exception as exc:  # noqa: BLE001 - keep the chat on screen if a turn fails
                notice = _public_turn_error(exc)
                audit.event("decision", {"error": notice})
                self._say("agent", notice)
                payload = self._payload()
                payload["notice"] = notice
                return payload
            if reply:
                self._say("agent", reply)
            elif not stopped:
                notice = "The assistant returned no reply. Your message is still in the conversation."
                payload = self._payload()
                payload["notice"] = notice
                return payload
            if stopped and session.closing_line and session.closing_line not in (reply or ""):
                self._say("agent", session.closing_line)
            return self._payload()

    def close(self) -> None:
        if self.audit is not None and self.session is not None and self.session.outcome is None:
            self.audit.finish(None)
        if self.api is not None:
            self.api.close()
        self.api = None
        self.session = None
        self.client = None
        self.audit = None
        self.messages = []

    def _say(self, role: str, text: str) -> None:
        if self.audit is not None:
            self.audit.say(role, text)
        self.messages.append(
            {
                "role": role,
                "text": text,
                "at": datetime.now().astimezone().strftime("%H:%M"),
            }
        )

    def _payload(self) -> dict:
        assert self.session is not None
        return {"ok": True, "messages": self.messages, "state": self.session.snapshot()}


OFFICE = Office()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/prodigal-logo.svg":
            body = (ROOT / "prodigal-logo.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/candidates":
            self._json(200, _candidate_catalog())
            return
        if path == "/api/activity":
            self._json(200, {"ok": True, "runs": _activity_rows()})
            return
        self._json(404, {"ok": False, "error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Send JSON."})
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/start":
            candidate_id = str(payload.get("candidate_id") or "").strip()
            if not candidate_id:
                self._json(400, {"ok": False, "error": "candidate_id is required."})
                return
            self._json(200, OFFICE.start(candidate_id))
            return
        if path == "/api/message":
            self._json(200, OFFICE.message(str(payload.get("text") or "")))
            return
        self._json(404, {"ok": False, "error": "Not found."})

    def log_message(self, fmt: str, *args) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


def _candidate_catalog() -> dict:
    """Read-only list for the dashboard. Phone numbers are not included."""
    _use_direct_connection()
    load_project_env()
    api = CandidateAPI()
    try:
        if not api.has_api_key():
            return _error("PRODIGAL_API_KEY is missing from .env.")
        records = _candidate_records(api.get_all_candidates())
    except CandidateAPIError as exc:
        return _error(_public_api_error(exc))
    finally:
        api.close()
    rows = []
    counts = {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "no_answer": 0,
        "refused": 0,
        "needs_human": 0,
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        status = str(record.get("collection_status") or "")
        if status in counts:
            counts[status] += 1
        done, total = progress_count(assess_record(record))
        rows.append(
            {
                "id": str(record.get("id") or ""),
                "name": str(record.get("full_name") or ""),
                "college": str(record.get("college") or ""),
                "collection_status": status,
                "done": done,
                "total": total,
                "last_contacted_at": str(record.get("last_contacted_at") or ""),
            }
        )
    rows.sort(key=lambda item: item["id"])
    return {"ok": True, "total": len(rows), "counts": counts, "candidates": rows}


def _activity_rows() -> list[dict]:
    """Audit file metadata only. Transcripts and phone numbers stay in the files."""
    if not RUNS_DIR.exists():
        return []
    rows = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        rows.append(
            {
                "file": path.name,
                "candidate_id": str(data.get("candidate_id") or ""),
                "started_at": str(data.get("started_at") or ""),
                "finished_at": str(data.get("finished_at") or ""),
                "final_outcome": data.get("final_outcome"),
                "events": len(data.get("events") or []),
            }
        )
    return rows


def _public_turn_error(exc: Exception) -> str:
    if isinstance(exc, CandidateAPIError):
        return _public_api_error(exc)
    text = str(exc).split("\n", 1)[0][:300]
    for name in ("PRODIGAL_API_KEY", "GEMINI_API_KEY"):
        secret = os.getenv(name) or ""
        if secret:
            text = text.replace(secret, "[redacted]")
    return text or "The assistant could not complete that turn."


def main() -> None:
    _use_direct_connection()
    load_project_env()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Completr AI site: http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
