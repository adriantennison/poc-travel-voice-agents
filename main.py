"""
Travel Call Center Voice Agents — Production-grade FastAPI service.

Handles inbound/outbound Twilio call flows for a travel call centre, with:
- API key authentication on all operational endpoints
- SQLite call state per CallSid (persists across turns)
- Back-office adapter: real httpx calls when API_BASE_URL is configured, demo fallback otherwise
- Twilio-shaped webhook (TwiML response + call state tracking)
- OpenAI itinerary parsing (optional, falls back to structured extraction)
- Configurable pricing multipliers via environment variables
- Structured JSON logging
"""

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from config import settings

# ── Structured logging ────────────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra"):
            payload.update(record.extra)
        return json.dumps(payload)


def _make_logger() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    log = logging.getLogger("travel-voice-agents")
    log.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    log.addHandler(handler)
    log.propagate = False
    return log


logger = _make_logger()


def _log(level: str, msg: str, **extra: Any) -> None:
    r = logging.LogRecord("travel-voice-agents", getattr(logging, level.upper(), 20), "", 0, msg, (), None)
    r.extra = extra  # type: ignore[attr-defined]
    logger.handle(r)


# ── Database — call state ─────────────────────────────────────────────────────

DB_PATH = Path(settings.DATABASE_URL.replace("sqlite:///", ""))
_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _migrate(_conn)
        _log("info", "Database initialised", path=str(DB_PATH))
    return _conn


def _migrate(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS call_sessions (
            call_sid TEXT PRIMARY KEY,
            customer_name TEXT,
            departure TEXT,
            destination TEXT,
            travel_date TEXT,
            cabin TEXT DEFAULT 'economy',
            budget REAL,
            status TEXT DEFAULT 'active',
            channel TEXT DEFAULT 'voice',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS call_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS outbound_jobs (
            id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            booking_reference TEXT,
            callback_window TEXT,
            status TEXT DEFAULT 'queued',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_sid ON call_events(call_sid);
    """)
    db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_call_session(call_sid: str, **fields: Any) -> dict[str, Any]:
    db = get_db()
    existing = db.execute("SELECT * FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
    if existing:
        updates = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [_now(), call_sid]
        db.execute(f"UPDATE call_sessions SET {updates}, updated_at = ? WHERE call_sid = ?", values)
    else:
        now = _now()
        db.execute(
            """INSERT INTO call_sessions (call_sid, customer_name, departure, destination,
               travel_date, cabin, budget, status, channel, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                call_sid,
                fields.get("customer_name"),
                fields.get("departure"),
                fields.get("destination"),
                fields.get("travel_date"),
                fields.get("cabin", "economy"),
                fields.get("budget"),
                fields.get("status", "active"),
                fields.get("channel", "voice"),
                now,
                now,
            ),
        )
    db.commit()
    row = db.execute("SELECT * FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
    return dict(row) if row else {}


def get_call_session(call_sid: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
    return dict(row) if row else None


def log_call_event(call_sid: str, event: str, detail: Any = None) -> None:
    get_db().execute(
        "INSERT INTO call_events (call_sid, event, detail) VALUES (?, ?, ?)",
        (call_sid, event, json.dumps(detail) if detail else None),
    )
    get_db().commit()


# ── Back-office adapter ───────────────────────────────────────────────────────

async def backoffice_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Make real httpx call when API_BASE_URL is configured; demo fallback otherwise."""
    base = settings.API_BASE_URL.rstrip("/")
    if not base or settings.INTEGRATION_MODE != "live":
        _log("info", f"[demo] backoffice {path}", payload=payload)
        return {"ok": True, "mode": "demo", "path": path, "received": payload}

    url = f"{base}{path}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.BACKOFFICE_API_KEY:
        headers["X-Api-Key"] = settings.BACKOFFICE_API_KEY

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        _log("info", f"[live] backoffice {path} → {resp.status_code}", status=resp.status_code)
        return {"ok": resp.is_success, "mode": "live", "status": resp.status_code, "data": data}
    except httpx.RequestError as exc:
        _log("error", f"[live] backoffice {path} failed", error=str(exc))
        return {"ok": False, "mode": "live", "path": path, "error": str(exc)}


# ── OpenAI itinerary parsing ──────────────────────────────────────────────────

async def parse_itinerary_with_ai(text: str) -> dict[str, Any] | None:
    """Parse a natural-language travel request into structured fields via OpenAI."""
    if not settings.OPENAI_API_KEY:
        return None

    prompt = (
        "Extract travel booking details from the text. Return ONLY valid JSON with keys: "
        "departure (IATA or city), destination (IATA or city), travel_date (YYYY-MM-DD or free text), "
        "passengers (integer), cabin (economy/premium/business), budget (number or null). "
        f'Text: "{text}"'
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "temperature": 0,
                    "max_tokens": 150,
                    "messages": [
                        {"role": "system", "content": "You are a travel data extractor. Return ONLY valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        if not resp.is_success:
            _log("warn", "OpenAI parse failed", status=resp.status_code)
            return None
        content = resp.json()["choices"][0]["message"]["content"].strip()
        parsed = json.loads(content)
        _log("info", "AI itinerary parsed", result=parsed)
        return parsed
    except Exception as exc:
        _log("warn", "OpenAI itinerary parsing error", error=str(exc))
        return None


# ── Auth + Twilio request validation ──────────────────────────────────────────

async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Validate API key. Bypass when API_KEY is not configured (dev mode)."""
    if not settings.API_KEY:
        return
    if x_api_key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Valid x-api-key header required")


def _twilio_signature_expected(request: Request, params: dict[str, Any]) -> str:
    """Compute Twilio request signature using the standard URL + sorted params algorithm."""
    token = settings.TWILIO_AUTH_TOKEN
    url = str(request.url)
    payload = url + ''.join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def require_twilio_signature(request: Request) -> None:
    """Validate X-Twilio-Signature when a Twilio auth token is configured.

    If TWILIO_AUTH_TOKEN is not configured, validation is bypassed for local/demo use.
    """
    if not settings.TWILIO_AUTH_TOKEN:
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Twilio-Signature header")

    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        params = dict(form)
    else:
        try:
            params = await request.json()
        except Exception:
            params = {}

    expected = _twilio_signature_expected(request, params)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid Twilio signature")


# ── Pydantic models ───────────────────────────────────────────────────────────

class InboundCall(BaseModel):
    departure: str
    destination: str
    travel_date: str
    passengers: int = Field(ge=1)
    budget: float | None = None
    cabin: Literal["economy", "premium", "business"] = "economy"
    call_sid: str | None = None
    customer_name: str | None = None


class OutboundRequest(BaseModel):
    reason: str
    customer_name: str
    booking_reference: str | None = None
    callback_window: str | None = None


class ReminderRequest(BaseModel):
    customer_name: str
    channel: Literal["sms", "email"]
    trip_summary: str


class NLParseRequest(BaseModel):
    text: str


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    get_db()  # initialise DB on startup
    yield


app = FastAPI(
    title="Travel Voice Agents",
    version="2.0.0",
    lifespan=lifespan,
)


# Request logger
@app.middleware("http")
async def log_requests(request: Request, call_next):
    _log("info", "request", method=request.method, path=str(request.url.path))
    response = await call_next(request)
    return response


# ── Health (no auth) ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "travel-voice-agents",
        "version": "2.0.0",
        "integrationMode": settings.INTEGRATION_MODE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Integration map ───────────────────────────────────────────────────────────

@app.get("/integrations", dependencies=[Depends(require_api_key)])
def integrations():
    return {
        "twilio_voice_webhook": "/twilio/voice-webhook",
        "crm_sync": "/backoffice/crm-sync",
        "reservation_handoff": "/backoffice/reservation-handoff",
        "outbound_jobs": "/voice/outbound-jobs",
        "nl_parse": "/voice/parse",
        "integration_mode": settings.INTEGRATION_MODE,
    }


# ── Voice inbound ─────────────────────────────────────────────────────────────

@app.post("/voice/inbound", dependencies=[Depends(require_api_key)])
async def inbound(call: InboundCall):
    call_sid = call.call_sid or f"CA_{uuid.uuid4().hex}"

    multiplier = {
        "economy": settings.PRICING_ECONOMY,
        "premium": settings.PRICING_PREMIUM,
        "business": settings.PRICING_BUSINESS,
    }[call.cabin]

    # Back-office adapter: fetch real options if configured
    backoffice_result = await backoffice_request(
        "/api/travel/options",
        {
            "departure": call.departure,
            "destination": call.destination,
            "travel_date": call.travel_date,
            "passengers": call.passengers,
            "cabin": call.cabin,
        },
    )

    # Use back-office data if live, otherwise use demo options
    if backoffice_result.get("mode") == "live" and backoffice_result.get("ok"):
        options = backoffice_result.get("data", {}).get("options", [])
    else:
        options = [
            {"carrier": "SkyBridge Air", "fare": round(640 * multiplier, 2), "currency": "USD"},
            {"carrier": "North Atlantic Connect", "fare": round(710 * multiplier, 2), "currency": "USD"},
        ]

    lowest = min((o["fare"] for o in options), default=0)

    # Persist call state
    session = upsert_call_session(
        call_sid,
        customer_name=call.customer_name,
        departure=call.departure,
        destination=call.destination,
        travel_date=call.travel_date,
        cabin=call.cabin,
        budget=call.budget,
        status="options_presented",
        channel="inbound",
    )
    log_call_event(call_sid, "options_presented", {"options": options, "options_source": backoffice_result.get("mode", "demo")})

    # CRM sync
    crm_payload = {
        "call_sid": call_sid,
        "customer_name": call.customer_name,
        "departure": call.departure,
        "destination": call.destination,
        "travel_date": call.travel_date,
        "cabin": call.cabin,
        "budget": call.budget,
    }
    crm_result = await backoffice_request("/api/crm/sync", crm_payload)

    _log("info", "Inbound call processed", call_sid=call_sid, departure=call.departure, destination=call.destination)

    return {
        "provider": "Twilio",
        "call_sid": call_sid,
        "summary": f"{call.passengers} passenger(s) from {call.departure} to {call.destination} on {call.travel_date}",
        "options": options,
        "options_source": backoffice_result.get("mode", "demo"),
        "budget_ok": call.budget is None or lowest <= call.budget,
        "handoff_required": any(o["fare"] > (call.budget or 10_000) for o in options),
        "next_step": "read itinerary options and offer live transfer",
        "crm_sync": {"endpoint": "/backoffice/crm-sync", "method": "POST", "result": crm_result},
        "reservation_handoff": {"endpoint": "/backoffice/reservation-handoff", "method": "POST"},
        "call_session": f"/voice/session/{call_sid}",
    }


# ── Natural language itinerary parse ─────────────────────────────────────────

@app.post("/voice/parse", dependencies=[Depends(require_api_key)])
async def parse_nl_itinerary(req: NLParseRequest):
    """Parse a natural-language travel request into structured data."""
    ai_result = await parse_itinerary_with_ai(req.text)
    if ai_result:
        return {"mode": "ai", "parsed": ai_result}

    # Fallback: simple keyword extraction
    text_lower = req.text.lower()
    cabin = "business" if "business" in text_lower else "premium" if "premium" in text_lower else "economy"
    return {
        "mode": "fallback",
        "parsed": {
            "departure": None,
            "destination": None,
            "travel_date": None,
            "passengers": 1,
            "cabin": cabin,
            "budget": None,
        },
        "note": "Set OPENAI_API_KEY for AI-powered extraction",
    }


# ── Voice outbound ─────────────────────────────────────────────────────────────

@app.post("/voice/outbound", dependencies=[Depends(require_api_key)])
async def outbound(req: OutboundRequest):
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    now = _now()

    # Store job in DB
    db = get_db()
    db.execute(
        """INSERT INTO outbound_jobs (id, customer_name, reason, booking_reference, callback_window, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (job_id, req.customer_name, req.reason, req.booking_reference, req.callback_window, now),
    )
    db.commit()

    email_followup = {
        "subject": f"Follow-up on your travel request: {req.customer_name}",
        "body": (
            f"We tried to reach you regarding {req.reason}. "
            "Reply with a good callback time and we will continue your booking support."
        ),
    }

    _log("info", "Outbound job created", job_id=job_id, customer=req.customer_name)

    return {
        "provider": "Twilio",
        "job_id": job_id,
        "reason": req.reason,
        "customer_name": req.customer_name,
        "booking_reference": req.booking_reference,
        "script": f"Hello {req.customer_name}, this is your travel assistant calling about {req.reason}.",
        "callback_window": req.callback_window or "next available slot",
        "email_followup": email_followup,
        "twilio_status_callback": "/twilio/status-callback",
        "job_url": f"/voice/outbound-jobs/{job_id}",
    }


@app.get("/voice/outbound-jobs", dependencies=[Depends(require_api_key)])
def list_outbound_jobs():
    rows = get_db().execute("SELECT * FROM outbound_jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return {"jobs": [dict(r) for r in rows]}


@app.get("/voice/session/{call_sid}", dependencies=[Depends(require_api_key)])
def get_session(call_sid: str):
    session = get_call_session(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="call_session_not_found")
    events = get_db().execute(
        "SELECT event, detail, created_at FROM call_events WHERE call_sid = ? ORDER BY id",
        (call_sid,),
    ).fetchall()
    return {"session": session, "events": [dict(e) for e in events]}


# ── Voice reminder ─────────────────────────────────────────────────────────────

@app.post("/voice/reminder", dependencies=[Depends(require_api_key)])
def reminder(req: ReminderRequest):
    _log("info", "Reminder sent", customer=req.customer_name, channel=req.channel)
    return {
        "provider": "Twilio" if req.channel == "sms" else "Email",
        "message": f"Hi {req.customer_name}, this is a reminder about {req.trip_summary}.",
        "channel": req.channel,
    }


# ── Twilio webhook (TwiML) ───────────────────────────────────────────────────

def _twiml_response(message: str, gather_action: str | None = None) -> str:
    if gather_action:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">{message}</Say>
  <Gather input="speech" action="{gather_action}" speechTimeout="auto">
    <Say voice="alice">Please tell me your departure, destination, and travel date.</Say>
  </Gather>
</Response>'''
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">{message}</Say>
</Response>'''


@app.post("/twilio/voice-webhook", dependencies=[Depends(require_twilio_signature)])
async def twilio_voice_webhook(request: Request):
    """
    Twilio-oriented inbound voice webhook.

    In live deployments Twilio posts form-encoded data with CallSid, From, To, CallStatus.
    Returns actual TwiML XML and persists call state.
    """
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    call_sid = payload.get("CallSid") or payload.get("call_sid") or f"CA_{uuid.uuid4().hex}"
    from_number = payload.get("From") or payload.get("from_number", "unknown")
    call_status = payload.get("CallStatus") or payload.get("call_status", "ringing")

    upsert_call_session(call_sid, channel="twilio-webhook", status=call_status)
    log_call_event(call_sid, "webhook_received", {"from": from_number, "status": call_status, "payload_keys": list(payload.keys())})

    _log("info", "Twilio webhook received", call_sid=call_sid, status=call_status)

    xml = _twiml_response(
        "Welcome to the travel assistance line. How can I help you today?",
        gather_action="/twilio/voice-gather",
    )

    return Response(content=xml, media_type="application/xml", headers={"X-Call-Sid": call_sid})


@app.post("/twilio/voice-gather", dependencies=[Depends(require_twilio_signature)])
async def twilio_voice_gather(request: Request):
    """Handle gathered speech and continue the Twilio call flow."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    call_sid = payload.get("CallSid") or payload.get("call_sid") or f"CA_{uuid.uuid4().hex}"
    speech_result = payload.get("SpeechResult") or payload.get("speech_result") or ""
    digits = payload.get("Digits") or payload.get("digits") or ""

    if call_sid:
        session = get_call_session(call_sid) or upsert_call_session(call_sid, channel="twilio-webhook", status="in-progress")
        log_call_event(call_sid, "speech_gathered", {"speech": speech_result, "digits": digits})
        if speech_result:
            session_name = session.get("customer_name") or "traveller"
            upsert_call_session(call_sid, customer_name=session_name, status="in-progress")

    _log("info", "Twilio gather received", call_sid=call_sid, speech=speech_result[:80], digits=digits)

    message = "Thanks — I’ve captured that. One moment while I check the best options."
    xml = _twiml_response(message)
    return Response(content=xml, media_type="application/xml", headers={"X-Call-Sid": call_sid})


@app.post("/twilio/status-callback", dependencies=[Depends(require_twilio_signature)])
async def twilio_status_callback(request: Request):
    """Handle Twilio status callbacks (call completed, failed, etc.)."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    call_sid = payload.get("CallSid") or payload.get("call_sid", "unknown")
    call_status = payload.get("CallStatus") or payload.get("call_status", "unknown")

    if call_sid != "unknown":
        upsert_call_session(call_sid, status=call_status)
        log_call_event(call_sid, "status_callback", {"status": call_status})

    _log("info", "Twilio status callback", call_sid=call_sid, status=call_status)
    return {"ok": True, "call_sid": call_sid, "call_status": call_status}


# ── Back-office endpoints ─────────────────────────────────────────────────────

@app.post("/backoffice/crm-sync", dependencies=[Depends(require_api_key)])
async def crm_sync(request: Request):
    payload = await request.json()
    result = await backoffice_request("/api/crm/sync", payload)
    _log("info", "CRM sync", mode=result.get("mode"))
    return {"ok": True, "system": "CRM", "action": "sync_contact_trip_request", "result": result}


@app.post("/backoffice/reservation-handoff", dependencies=[Depends(require_api_key)])
async def reservation_handoff(request: Request):
    payload = await request.json()
    result = await backoffice_request("/api/reservations/handoff", payload)
    _log("info", "Reservation handoff", mode=result.get("mode"))
    return {"ok": True, "system": "reservation_desk", "action": "handoff_create", "result": result}
