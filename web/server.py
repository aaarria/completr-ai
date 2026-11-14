"""Local chat site that uses the same agent and Prodigal API as the terminal.

Run: python -m web.server
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.conversation import GeminiClient, GeminiError, _identity_answer
from agent.session import Session
from services.audit_log import AuditLog
from services.candidate_api import CandidateAPI, CandidateAPIError, load_project_env

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "index.html").read_text(encoding="utf-8")
PORT = int(os.getenv("PORT", "8000"))


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
                return _error(str(exc))
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
            if session.all_valid():
                session.mark_already_complete()
                self._say("agent", session.closing_line)
                return self._payload()
            greeting = session.greeting()
            self._say("agent", greeting)
            self.client = GeminiClient(gemini_key, session, audit)
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
                audit.event("decision", {"error": str(exc)})
                self._say("agent", str(exc))
                return self._payload()
            if reply:
                self._say("agent", reply)
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
        self.messages.append({"role": role, "text": text})

    def _payload(self) -> dict:
        assert self.session is not None
        return {"ok": True, "messages": self.messages, "state": self.session.snapshot()}


OFFICE = Office()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in {"/", "/index.html"}:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Completr AI site: http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
