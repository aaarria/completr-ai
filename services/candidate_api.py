"""Client for the Prodigal Candidate Database API.

Public:
    GET /v1/schema

Protected (Bearer token):
    GET /v1/candidates
    GET /v1/candidates/{candidate_id}
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

# services/candidate_api.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_project_env() -> bool:
    """Load the project .env file, wherever the command was started from.

    Returns True when python-dotenv found the file. A value already exported
    in the shell is kept and is not overwritten.
    """
    return load_dotenv(ENV_PATH)


class CandidateAPIError(Exception):
    """The Candidate Database API could not complete the request.

    Messages from this error never include the API key.
    """


class CandidateAPI:
    """Small wrapper around the Prodigal Candidate Database API.

    Reads PRODIGAL_BASE_URL and PRODIGAL_API_KEY from the environment
    (or from a .env file) unless you pass them in directly.
    """

    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        load_project_env()

        resolved_base_url = base_url if base_url is not None else os.getenv("PRODIGAL_BASE_URL", "")
        resolved_api_key = api_key if api_key is not None else os.getenv("PRODIGAL_API_KEY", "")

        self.base_url = (resolved_base_url or "").strip().rstrip("/")
        self._api_key = (resolved_api_key or "").strip()
        self.timeout = timeout
        self.env_file = ENV_PATH
        self.env_file_found = ENV_PATH.is_file()

        if not self.base_url:
            raise CandidateAPIError(
                "PRODIGAL_BASE_URL is missing. Copy .env.example to .env and set it."
            )

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def get_schema(self) -> Any:
        """Return the public database schema. No API key required."""
        return self._request("GET", "/v1/schema", authenticated=False)

    def get_all_candidates(self) -> Any:
        """Return every candidate. Requires PRODIGAL_API_KEY."""
        return self._request("GET", "/v1/candidates", authenticated=True)

    def list_status_rows(self) -> list[tuple[str, str]]:
        """Return only (candidate id, collection_status) for each candidate.

        Names, phone numbers, emails, and other profile fields are dropped
        here so a caller cannot print them by accident.
        """
        records = _candidate_records(self.get_all_candidates())
        rows: list[tuple[str, str]] = []
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise CandidateAPIError(
                    f"Candidate {index} from GET /v1/candidates was not an object."
                )
            candidate_id = record.get("id")
            if candidate_id is None or not str(candidate_id).strip():
                raise CandidateAPIError(
                    f"Candidate {index} from GET /v1/candidates did not include an id."
                )
            status = record.get("collection_status")
            if status is None or not str(status).strip():
                status_text = "not set"
            else:
                status_text = str(status)
            rows.append((str(candidate_id).strip(), status_text))
        return rows

    def has_api_key(self) -> bool:
        """True when a non-empty Bearer token is configured. The key itself is never returned."""
        return bool(self._api_key)

    def get_candidate(self, candidate_id: str) -> Any:
        """Return one candidate by id. Requires PRODIGAL_API_KEY.

        The id must come from the API (for example from get_all_candidates).
        This method does not invent or guess ids.
        """
        candidate_id = str(candidate_id).strip()
        if not candidate_id:
            raise CandidateAPIError("candidate_id is required.")

        safe_id = quote(candidate_id, safe="")
        return self._request("GET", f"/v1/candidates/{safe_id}", authenticated=True)

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def _request(self, method: str, path: str, authenticated: bool) -> Any:
        """Perform one HTTP call and return parsed JSON."""
        headers: dict[str, str] = {}
        if authenticated:
            if not self._api_key:
                raise CandidateAPIError(
                    "PRODIGAL_API_KEY is missing. Add it to your .env file. "
                    "Candidate endpoints require a Bearer token; /v1/schema does not."
                )
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self.base_url}{path}"

        try:
            response = self._session.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise CandidateAPIError(
                f"{method} {path} timed out after {self.timeout} seconds."
            ) from exc
        except requests.ConnectionError as exc:
            raise CandidateAPIError(
                f"Could not connect to the Candidate Database API ({method} {path})."
            ) from exc
        except requests.HTTPError as exc:
            raise CandidateAPIError(self._http_error_message(method, path, exc)) from exc
        except requests.RequestException as exc:
            raise CandidateAPIError(f"{method} {path} failed before a response was received.") from exc

        if response.status_code == 204 or not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise CandidateAPIError(
                f"{method} {path} returned a response that was not valid JSON."
            ) from exc

    def _http_error_message(self, method: str, path: str, exc: requests.HTTPError) -> str:
        """Build an error string that is safe to print."""
        response = exc.response
        status = response.status_code if response is not None else "unknown"
        message = f"Candidate API returned HTTP {status} for {method} {path}."

        detail = self._response_detail(response)
        if detail:
            message = f"{message} {detail}"
        return message

    def _response_detail(self, response: requests.Response | None) -> str:
        """Pull a short error detail from the body, with the API key removed."""
        if response is None:
            return ""

        detail = ""
        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            for key in ("detail", "message", "error"):
                value = body.get(key)
                if value:
                    detail = str(value)
                    break
            if not detail:
                detail = str(body)
        elif body is not None:
            detail = str(body)
        else:
            detail = (response.text or "").strip()

        return self._redact(detail)[:300]

    def _redact(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "[redacted]")
        return text


def _candidate_records(payload: Any) -> list[Any]:
    """Accept the list shapes this API might use, without guessing record contents."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("candidates", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        keys = ", ".join(sorted(str(key) for key in payload)) or "(none)"
        raise CandidateAPIError(
            "GET /v1/candidates returned an object without a candidate list. "
            f"Top-level keys: {keys}."
        )
    raise CandidateAPIError(
        "GET /v1/candidates returned "
        f"{type(payload).__name__}, which is not a candidate list."
    )
