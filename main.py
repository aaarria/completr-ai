"""CandidateIQ entry point.

Checks that .env loaded, calls the live Candidate Database API, and can list
candidate ids with their collection statuses. It does not print API keys or
profile fields.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from services.candidate_api import CandidateAPI, CandidateAPIError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the Prodigal Candidate Database API connection."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("schema", "candidates"),
        default="candidates",
        help=(
            "schema: call GET /v1/schema. "
            "candidates (default): list each candidate id and collection_status."
        ),
    )
    args = parser.parse_args(argv)

    api: CandidateAPI | None = None
    try:
        api = CandidateAPI()
        _print_env_status(api)
        if args.command == "schema":
            _print_schema(api.get_schema())
            return 0
        _print_schema(api.get_schema())
        print()
        return _print_candidates(api)
    except CandidateAPIError as exc:
        print(f"API error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if api is not None:
            api.close()


def _print_env_status(api: CandidateAPI) -> None:
    location = "found" if api.env_file_found else "not found"
    print(f".env: {location} ({api.env_file})", flush=True)
    print(f"PRODIGAL_BASE_URL: {api.base_url}", flush=True)
    if api.has_api_key():
        print("PRODIGAL_API_KEY: loaded", flush=True)
    else:
        print("PRODIGAL_API_KEY: missing", flush=True)


def _print_schema(schema: Any) -> None:
    print("GET /v1/schema: ok", flush=True)
    if not isinstance(schema, dict):
        print(f"Schema response type: {type(schema).__name__}", flush=True)
        return

    statuses = schema.get("collection_statuses")
    mandatory = schema.get("mandatory_fields")
    if isinstance(statuses, list):
        print("Collection statuses: " + ", ".join(str(item) for item in statuses), flush=True)
    if isinstance(mandatory, list):
        print(f"Mandatory fields: {len(mandatory)}", flush=True)


def _print_candidates(api: CandidateAPI) -> int:
    if not api.has_api_key():
        print(
            "Cannot call GET /v1/candidates: PRODIGAL_API_KEY is empty. "
            "Paste your personal key into .env and run this again.",
            file=sys.stderr,
            flush=True,
        )
        return 1

    rows = api.list_status_rows()
    print(f"GET /v1/candidates: {len(rows)} candidate(s)", flush=True)
    if not rows:
        print("No candidates in this workspace.", flush=True)
        return 0

    id_width = max(len("id"), *(len(candidate_id) for candidate_id, _ in rows))
    print(f"{'id':<{id_width}}  collection_status", flush=True)
    for candidate_id, status in rows:
        print(f"{candidate_id:<{id_width}}  {status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
