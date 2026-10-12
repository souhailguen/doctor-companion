# Doctor Companion — AI medical secretary (MVP)

An AI secretary for a doctor's practice that books appointments through two channels:

- **Web**: an Arabic (RTL) calendar where patients pick a free slot, book, look up and cancel appointments.
- **Phone**: a Claude-powered voice agent that answers calls in Arabic (mirroring the caller's dialect, e.g. Darija), checks availability, books, finds and cancels appointments.

Both channels share one scheduler and one SQLite database, so a slot booked on the web disappears for callers immediately (and vice versa). A unique index makes double booking impossible even when both race for the same slot.

## Architecture

```
            ┌──────────────┐        ┌───────────────────────────┐
 Patient ──▶│ Web calendar │──REST─▶│                           │
            └──────────────┘        │  FastAPI  (app/main.py)   │
            ┌──────────────┐        │                           │      ┌──────────┐
 Caller ──▶ │ Telephony    │─text──▶│  /voice/calls/...         │─────▶│ Claude   │
   (voice)  │ adapter: STT │◀─text──│   SecretaryAgent          │◀─────│ tool use │
            │ + TTS        │        │   (app/agent.py)          │      └──────────┘
            └──────────────┘        │        │                  │
                                    │   Scheduler ── SQLite     │
                                    └───────────────────────────┘
```

| File | Role |
|---|---|
| `app/config.py` | Clinic name, doctor, timezone, opening hours, slot length, model settings |
| `app/scheduling.py` | Booking rules: slot generation, availability, book / cancel / lookup |
| `app/agent.py` | The secretary: system prompt, tools (`check_availability`, `book_appointment`, `find_appointments`, `cancel_appointment`, `end_call`), Claude tool-use loop |
| `app/main.py` | REST API for the web, provider-agnostic voice gateway, admin endpoint |
| `app/static/` | `index.html` booking calendar, `call.html` call simulator |

### Voice gateway (provider-agnostic)

The telephony provider is not chosen yet, so the phone channel is a plain text API. Any provider adapter (Twilio, Vonage, a SIP trunk + STT/TTS) only has to:

1. On an incoming call: `POST /voice/calls {"caller_phone": "+2126..."}` → speak `reply`, keep `call_id`.
2. For every caller utterance (after speech-to-text): `POST /voice/calls/{call_id}/turn {"text": "..."}` → speak `reply`; hang up when `end_call` is `true`.

The `/call` page is a browser simulator of that adapter: it uses the browser's speech recognition (Chrome, `ar-MA` / `ar-SA` / `ar-EG`) and speech synthesis, or you can type.

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # set ANTHROPIC_API_KEY and clinic details, then export them
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
```

- Booking calendar: http://localhost:8000/
- Call simulator: http://localhost:8000/call
- API docs: http://localhost:8000/docs
- Clinic day view: `GET /api/admin/appointments?day=2026-10-13` with header `X-Admin-Token: $ADMIN_TOKEN` (disabled unless `ADMIN_TOKEN` is set)

The web calendar works without an API key; only the phone agent calls Claude.

Tests (no network needed — the Claude API is replaced by a scripted fake):

```bash
pytest
```

## Configuration

| Variable | Default | |
|---|---|---|
| `CLINIC_NAME`, `DOCTOR_NAME` | placeholders | Used in the greeting and prompt |
| `CLINIC_TIMEZONE` | `Africa/Casablanca` | All times are clinic local time |
| `SLOT_MINUTES` | `30` | Appointment length |
| `BOOKING_HORIZON_DAYS` | `60` | How far ahead patients may book |
| `DATABASE_PATH` | `clinic.db` | SQLite file |
| `CLAUDE_MODEL` | `claude-opus-5-5` | |
| `CLAUDE_EFFORT` | `low` | Kept low for phone latency; raise if conversations go wrong |
| `CLAUDE_FALLBACKS` | `1` | Server-side refusal fallback (beta); `0` disables |
| `ADMIN_TOKEN` | unset | Enables the admin endpoint |

Opening hours are in `DEFAULT_WORKING_HOURS` in `app/config.py` (Mon–Thu 9–13 & 14–18, Fri 9–12 & 15–18, Sat 9–13, Sun closed).

## Next steps after the MVP

- **Telephony adapter**: pick a provider and write the adapter. With Twilio, the simplest path is `<Gather input="speech" language="ar-MA">` webhooks mapped onto the two endpoints above; for lower latency and barge-in, a streaming setup (Twilio ConversationRelay / Media Streams with a streaming STT/TTS) on the same agent.
- **Arabic speech quality**: evaluate STT/TTS vendors on real dialect recordings — this matters more than the LLM for caller experience.
- **Persistence**: conversations are in memory (2h TTL); move them to Redis/DB for multiple server instances.
- **Notifications**: SMS/WhatsApp confirmation and reminders after a booking.
- **Staff side**: authenticated dashboard for the doctor/assistant, blocking days off and holidays.
- **Compliance**: health data is sensitive — add auth, audit logs, encryption at rest, data-retention rules, and check local regulations (e.g. Morocco's law 09-08 / CNDP) before going live.
