# Travel Call Center Voice Agents

Production-grade FastAPI API for a travel call centre handling inbound and outbound AI voice agent flows via Twilio.

## Features

- **Twilio voice webhook**: Accepts inbound calls with TwiML-shaped response and call state tracking
- **Call state persistence**: SQLite-backed per-CallSid session storage across turns
- **Back-office adapter**: Real httpx calls when `API_BASE_URL` is configured; transparent demo fallback
- **AI itinerary parsing**: OpenAI-powered natural-language extraction of travel details (optional, keyword fallback)
- **Outbound job queue**: Stores callback jobs with full audit trail
- **API key authentication** on all operational endpoints
- **Configurable pricing multipliers** via environment variables
- **Structured JSON logging** for all requests and events
- **Twilio status callbacks** with state updates

## Stack

- Python 3.13+
- FastAPI / Uvicorn
- Pydantic v2
- httpx (async back-office calls)
- SQLite (call state + outbound jobs)
- python-dotenv

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your credentials

uvicorn main:app --reload
```

## Docker

```bash
docker build -t travel-voice-agents .
docker run -p 8000:8000 --env-file .env travel-voice-agents
```

## Testing

```bash
pytest test_main.py -v
```

20 test cases cover:
- Inbound call processing with options
- Call state persistence across requests
- Budget and pricing validation
- Twilio webhook handling (JSON and form-encoded)
- Twilio status callback state updates
- Back-office demo/live modes
- Outbound job creation and listing
- NL parse fallback
- Reminder endpoints
- Input validation (422 on bad requests)
- Session not found (404)
- TwiML XML response shape
- Gather endpoint flow continuation

## Integration Modes

| Mode | Behaviour |
|------|-----------|
| `demo` | All back-office calls return simulated responses. No external HTTP. Safe for development. |
| `live` | Real httpx requests to `API_BASE_URL`. Falls back gracefully with structured error responses. |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_KEY` | Recommended | — | x-api-key auth. If unset, auth is bypassed. |
| `INTEGRATION_MODE` | No | `demo` | `demo` or `live` |
| `API_BASE_URL` | For live | — | Back-office API base URL |
| `BACKOFFICE_API_KEY` | For live | — | Back-office API key |
| `TWILIO_ACCOUNT_SID` | For live | — | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | For live | — | Twilio auth token |
| `TWILIO_PHONE_NUMBER` | For live | — | Twilio phone number |
| `OPENAI_API_KEY` | No | — | Enables AI itinerary parsing |
| `DATABASE_URL` | No | `sqlite:///calls.db` | SQLite database path |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `PRICING_ECONOMY_MULTIPLIER` | No | `1.0` | Economy fare multiplier |
| `PRICING_PREMIUM_MULTIPLIER` | No | `1.35` | Premium fare multiplier |
| `PRICING_BUSINESS_MULTIPLIER` | No | `1.9` | Business fare multiplier |

## API Endpoints

### Health (no auth)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health + integration mode |

### Voice

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/voice/inbound` | Yes | Process inbound call — returns options, creates call session |
| `POST` | `/voice/outbound` | Yes | Create outbound callback job |
| `GET` | `/voice/outbound-jobs` | Yes | List queued outbound jobs |
| `GET` | `/voice/session/{call_sid}` | Yes | Retrieve call session + events |
| `POST` | `/voice/parse` | Yes | Parse natural-language itinerary request |
| `POST` | `/voice/reminder` | Yes | Send SMS/email reminder |

### Twilio

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/twilio/voice-webhook` | Signature | Inbound Twilio webhook — TwiML XML + state tracking |
| `POST` | `/twilio/voice-gather` | Signature | Gathered speech handler for continuing the call flow |
| `POST` | `/twilio/status-callback` | Signature | Twilio status update callback |

### Back-office

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/backoffice/crm-sync` | Yes | Sync trip request to CRM |
| `POST` | `/backoffice/reservation-handoff` | Yes | Create reservation handoff |
| `GET` | `/integrations` | Yes | Integration map and mode |

## Limitations (Acknowledged Scope)

- **Demo options are hardcoded carriers** (SkyBridge Air, North Atlantic Connect). In `live` mode, options come from the back-office API.
- **AI parsing requires OPENAI_API_KEY**. Falls back to keyword extraction otherwise.
- **Rate limiting** not included — use a reverse proxy.

## License

Proprietary — Neo Claw Ltd.
