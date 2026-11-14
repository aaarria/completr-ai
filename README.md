# Completr AI

Intelligent Application Completion Through Conversation.

Completr AI greets one internship candidate by name, uses Gemini function calling to collect missing or invalid fields, validates every answer in Python, and writes accepted values to the Prodigal Candidate Database.

Repository: https://github.com/aaarria/completr-ai

## Setup and run

Add `PRODIGAL_API_KEY` and `GEMINI_API_KEY` to `.env` after the copy. Do not commit `.env`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py --candidate-id <id>
python -m web.server
```

The fourth command runs the terminal agent. The fifth command serves the same agent at http://127.0.0.1:8000. Type `/quit` to leave a chat. `python main.py candidates` lists ids and statuses only.

## Architecture

Python owns validation, status changes, and every PATCH. Gemini only chooses the next question and calls tools.

1. `GET /v1/candidates/{id}` and `GET /v1/schema`.
2. `Session.begin()` reads `collection_status`. `completed` does not start a chat. A `pending` record whose mandatory fields are already valid is saved as `completed` and checked with a fresh GET. No other stored status is overwritten.
3. Otherwise the terminal or site greets the candidate by name and asks them to confirm identity. The id is never requested.
4. The agent loop sends the conversation to `gemini-3.5-flash-lite`. If the model calls a tool, Python runs it and returns the tool result. The loop repeats until the session stops. `gemini-3.1-flash-lite` is the fallback model.
5. Accepted fields go out with `PATCH`, `If-Match`, and a new idempotency key. A 409 rereads the record and retries once. A fresh GET checks the write.

### Tools

| Tool | Purpose |
| --- | --- |
| `confirm_identity` | Record whether the person is the candidate who was greeted. |
| `get_application_state` | Read which fields are still open. |
| `record_candidate_fields` | Extract one or many fields, each with an exact quote. Python validates them. |
| `end_conversation` | Close as `completed`, `in_progress`, `no_answer`, `refused`, or `needs_human`. |

### Where state lives

- During a chat, state is the in-memory `Session` in `agent/session.py`: the latest record, field assessments, identity, evidence, and whether the chat is still open. Gemini's message history stays in that process.
- The durable application is the candidate record in the Prodigal database. Notes, `collection_status`, and `attempt_count` are how a later session knows what already happened.
- `runs/<id>_<timestamp>_<uuid>.json` is the audit file: transcript, tool calls, validation decisions, and API requests and responses. Phone numbers and API keys are removed before it is written.
- The website keeps one `Session` in memory. The local demo candidate never leaves this process and is never inserted into Prodigal.

## Exceptions

Rules live in `services/validation.py`, not in the model. Phone is read-only and is never asked for, shown, or patched. Optional LinkedIn, GitHub, and skills do not block completion.

| Situation | What happens |
| --- | --- |
| No response | `/quit` or leaving before any application answer saves `no_answer` and increases `attempt_count` once for that conversation. |
| Continuing later | Answers already accepted are patched first. Status becomes `in_progress`, and the note lists the remaining fields. |
| Refusal | Refusing a mandatory field saves `refused` and names that field. Refusing an optional field leaves the chat open. |
| Invalid or ambiguous answer | The value is not stored. A bad email or year is asked again. A range such as "around 8 or 9" is clarification. Two unresolved clarifications on one field become `needs_human`. |
| Existing incorrect data | A valid stored value is kept. It changes only when the candidate confirms the replacement. A disputed identity stops the chat as `needs_human` and blocks later writes in that session. |

## What works

Verified on the live API, with the transcripts in `runs/`:

- `c004`: one sentence saved degree, branch, and college. Status `completed`. Tool calls and PATCH bodies are in `runs/c004_20261114T145112_6282e1.json`.
- `c001`: browser chat confirmed identity, then saved email and CGPA. Status `completed`. See `runs/c001_20261114T151722_b8307e.json`.
- `c002`: an in-progress session later reached `completed`. See `runs/c002_20261114T153203_1f29de.json`.
- `c005` corrected an invalid email. `c008` corrected an invalid graduation year and a missing CGPA.
- `c003` and `c006` were already valid and pending, so they were completed without a chat and checked with a fresh GET.
- `c007`: `/quit` before any answers saved `no_answer`. It was later reopened to `pending` with a status-only PATCH. Phone, notes, and `attempt_count` stayed the same.

Continue later, mandatory refusal, optional refusal, no answer, identity dispute, and two ambiguous CGPA answers are covered by `tests/test_exceptions.py` and `tests/test_status.py` with a fake database. Those tests do not write to Prodigal.

## What does not

- The website holds one conversation in memory. Restarting the server drops that chat and the local demo record.
- Free-tier Gemini quota can stop a chat mid-session. The error is shown and written to the audit file. Nothing is invented to fill the gap.
- The model can still ask an awkward follow-up. Python decides what is stored.
- A live refusal, continue-later, and `needs_human` path were not run again against Prodigal after the mocked tests. The local demo log `runs/demo_20261114T161154_d6985a.json` is one `needs_human` conversation, and it never called the live API.

## What I would build next

- Persist the website session so a refresh can resume the same candidate without restarting the process.
- A reviewer queue for `needs_human`, with the evidence quotes beside the disputed field.
- Retry the Gemini call when the free-tier limit clears, without sending a second PATCH for a save that already succeeded.
