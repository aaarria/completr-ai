# Completr AI

Intelligent Application Completion Through Conversation.

Completr AI talks with one internship candidate, checks the application in Python, and writes accepted answers to the Prodigal Candidate Database.

## Setup and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put `PRODIGAL_API_KEY` and `GEMINI_API_KEY` in `.env`. Do not commit that file.

Terminal agent:

```bash
python main.py --candidate-id <id>
```

Local site, same agent and API:

```bash
python -m web.server
```

Open http://127.0.0.1:8000. Type `/quit` to end a chat. List ids without profile details:

```bash
python main.py candidates
```

## Architecture

1. `GET /v1/candidates/{id}` and `GET /v1/schema`.
2. Python classifies each mandatory field as valid, missing, invalid, or needs clarification.
3. If every mandatory field is valid, no chat starts.
4. Otherwise the terminal or site greets the candidate by name and confirms identity. The id is never requested.
5. Gemini (`gemini-3.5-flash-lite`) decides the next step and calls tools. Python validates every extracted value before a save.
6. Changed writable fields are sent with `PATCH`, `If-Match`, and a new idempotency key. A 409 rereads the record and retries. A fresh GET checks the write.

State lives in the candidate record plus the audit file in `runs/`.

## Gemini tools

| Tool | Purpose |
| --- | --- |
| `confirm_identity` | Record whether the person is the candidate who was greeted. |
| `get_application_state` | Read which fields are still open. |
| `record_candidate_fields` | Extract one or many fields, including an exact quote. Python validates them. |
| `end_conversation` | Save `completed`, `in_progress`, `no_answer`, `refused`, or `needs_human`. |

## Validation

Rules live in `services/validation.py`, not in the model. Phone is read-only. Optional LinkedIn, GitHub, and skills do not block completion. A valid stored value is not replaced unless the candidate confirms the change. A range such as "around 8 or 9" is clarification, not a save. Two unresolved clarifications on the same field escalate to `needs_human`.

## Exceptions

| Situation | Status |
| --- | --- |
| All mandatory fields valid | `completed` |
| Continue later, or `/quit` after some answers | `in_progress`, with remaining fields in notes |
| Refuses a mandatory field | `refused`, field named in notes |
| Leaves before giving application information | `no_answer`, `attempt_count` increased |
| Wrong person, upset candidate, or unresolved contradiction | `needs_human` |

Optional refusals do not stop completion. Progress is saved before an interrupted chat ends.

## Database safety

Bearer authentication. `If-Match` uses the version just read. Each PATCH has its own idempotency key. The body never includes `id`, `phone`, or `version`. A 409 fetches the latest record, drops fields that already match, and retries once.

## Differentiators

- One message can fill several fields. Each field is validated on its own, then the outstanding list is recalculated.
- Ambiguous, invalid, and contradictory answers are separated. Disputed valid data is not overwritten silently.
- Each extracted field is logged with the candidate's quote, the validation result, and the save, reject, or clarification decision. API keys and phone numbers are removed from logs.

## What works

Verified on the live API:

- `c003`: already complete, no chat, status set to `completed`, fresh GET matched.
- `c004`: one sentence saved degree, branch, and college. Status `completed`.
- `c005`: invalid email corrected and location saved. Status `completed`.
- `c008`: invalid graduation year and missing CGPA corrected. Status `completed`.
- `c007`: `/quit` before any answers saved `no_answer`.

`gemini-3.5-flash-lite` returned a real `record_candidate_fields` tool call after `thinking_budget` was removed. The earlier `gemini-3.5-flash` free-tier daily quota is exhausted.

## Known limits

- `c001` (ambiguous CGPA, then refusal) and the continue-later closeout for `c002` did not finish. `c002` did save an available-from date as `in_progress` before the session was cut off.
- The site keeps one session in memory. Restart the server to switch cleanly after a crash.
- The model can still ask an awkward follow-up. Python, not the model, decides what is stored.
- Free-tier Gemini limits can stop a chat mid-session. The error is printed and logged.

## Sample runs

`runs/c004_20261114T145112_6282e1.json` and `runs/c005_20261114T145204_09281b.json` are completed live sessions. Phone numbers in the logs are redacted.
