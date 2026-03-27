# Travel Voice Agents — Twilio Call Center POC

Inbound/outbound voice-agent POC for a travel agency handling qualification, itinerary collection, callback workflows, and reminder automation.

## Demonstrates
- Inbound call intake for flight search requests
- Outbound callback and reminder script generation
- Concrete Twilio webhook and back-office integration surfaces
- API-ready itinerary capture payloads
- Escalation rules for handoff to human agents
- Email follow-up payload when a callback is unanswered

## Stack
- Python 3.11+
- FastAPI

## Run
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Test
```bash
pytest -q
```

## Key endpoints
- `GET /health`
- `GET /integrations`
- `POST /voice/inbound`
- `POST /voice/outbound`
- `POST /voice/reminder`
- `POST /twilio/voice-webhook`
- `POST /backoffice/crm-sync`
- `POST /backoffice/reservation-handoff`
