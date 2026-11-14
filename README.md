# CandidateIQ

Autonomous agent that completes incomplete internship applications by talking with candidates and updating the Prodigal Candidate Database API.

This first step is only the API connection. There is no chatbot and no Gemini integration yet.

## Layout

| Path | Purpose |
| --- | --- |
| `main.py` | Loads `.env`, checks the schema, and lists candidate ids with collection statuses. |
| `services/candidate_api.py` | `CandidateAPI` client for the Candidate Database API. |
| `services/__init__.py` | Marks `services` as a Python package. |
| `runs/` | Empty folder for later run artifacts. |
| `tests/` | Empty folder for later tests. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Template for required environment variables. Never put real keys here. |
| `.gitignore` | Keeps `.env`, virtualenvs, and generated files out of git. |

## Setup

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set:

- `PRODIGAL_BASE_URL` — already filled in `.env.example`. Leave it unless Prodigal gives you a different host.
- `PRODIGAL_API_KEY` — Bearer token for `/v1/candidates` and `/v1/candidates/{candidate_id}`. The schema endpoint does not use it.
- `GEMINI_API_KEY` — leave blank for now. Nothing in this step reads it.

Do not commit `.env`. It is listed in `.gitignore`.

## Check the connection

From the project root, with the virtualenv active:

```bash
python main.py schema
python main.py candidates
```

`python main.py` does both. It prints whether `.env` loaded and whether the API key is present, then the live schema summary, then each candidate `id` and `collection_status`. It does not print the key, names, phone numbers, or other profile fields.

## API client

`CandidateAPI` in `services/candidate_api.py`:

- `get_schema()` — `GET /v1/schema` (no auth)
- `get_all_candidates()` — `GET /v1/candidates` (Bearer token)
- `list_status_rows()` — same call, reduced to id and collection status
- `get_candidate(candidate_id)` — `GET /v1/candidates/{candidate_id}` (Bearer token)

Pass a `candidate_id` that came from the API. Network timeouts, connection failures, and HTTP errors raise `CandidateAPIError`. Error text never includes the API key.
