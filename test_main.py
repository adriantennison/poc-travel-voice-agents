"""Tests for Travel Voice Agents API."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite:///test_calls.db")
os.environ.setdefault("INTEGRATION_MODE", "demo")
os.environ.setdefault("API_KEY", "")  # disable auth in tests

from main import app  # noqa: E402

client = TestClient(app)


# ── Health ────────────────────────────────────────────────────────────────────

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["version"] == "2.0.0"
    assert "timestamp" in body


# ── Inbound voice ─────────────────────────────────────────────────────────────

def test_inbound_returns_options_and_surfaces():
    r = client.post(
        "/voice/inbound",
        json={
            "departure": "LHR",
            "destination": "DXB",
            "travel_date": "2026-04-15",
            "passengers": 2,
            "budget": 1500,
            "cabin": "premium",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "Twilio"
    assert len(body["options"]) >= 1
    assert body["crm_sync"]["endpoint"] == "/backoffice/crm-sync"
    assert body["reservation_handoff"]["endpoint"] == "/backoffice/reservation-handoff"
    assert "call_sid" in body
    assert "call_session" in body


def test_inbound_creates_call_session():
    r = client.post(
        "/voice/inbound",
        json={
            "departure": "JFK",
            "destination": "LAX",
            "travel_date": "2026-05-01",
            "passengers": 1,
            "cabin": "economy",
            "call_sid": "CA_test_session_001",
        },
    )
    assert r.status_code == 200
    call_sid = r.json()["call_sid"]

    session_r = client.get(f"/voice/session/{call_sid}")
    assert session_r.status_code == 200
    body = session_r.json()
    assert body["session"]["call_sid"] == call_sid
    assert body["session"]["departure"] == "JFK"
    assert len(body["events"]) >= 1


def test_inbound_budget_check():
    r = client.post(
        "/voice/inbound",
        json={
            "departure": "SYD",
            "destination": "SIN",
            "travel_date": "2026-06-01",
            "passengers": 1,
            "budget": 100,  # very low budget — handoff required
            "cabin": "economy",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["budget_ok"] is False
    assert body["handoff_required"] is True


def test_inbound_business_class_pricing():
    r = client.post(
        "/voice/inbound",
        json={
            "departure": "LHR",
            "destination": "NYC",
            "travel_date": "2026-07-01",
            "passengers": 1,
            "cabin": "business",
        },
    )
    assert r.status_code == 200
    options = r.json()["options"]
    # Business multiplier >= 1.9x economy
    assert all(o["fare"] >= 640 * 1.9 - 1 for o in options)


# ── NL parse ──────────────────────────────────────────────────────────────────

def test_nl_parse_fallback():
    r = client.post("/voice/parse", json={"text": "I need two business class seats to Tokyo"})
    assert r.status_code == 200
    body = r.json()
    assert body["parsed"]["cabin"] == "business"
    assert body["parsed"]["passengers"] == 1  # fallback doesn't parse number
    assert "mode" in body


# ── Outbound ──────────────────────────────────────────────────────────────────

def test_outbound_creates_job():
    r = client.post(
        "/voice/outbound",
        json={
            "reason": "booking_confirmation",
            "customer_name": "Alice Smith",
            "booking_reference": "TRV-8842",
            "callback_window": "09:00-12:00 GMT",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "Twilio"
    assert body["customer_name"] == "Alice Smith"
    assert "job_id" in body
    assert "job_url" in body
    assert "Alice Smith" in body["script"]


def test_outbound_job_listed():
    r = client.post(
        "/voice/outbound",
        json={"reason": "trip_reminder", "customer_name": "Bob Jones"},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    jobs_r = client.get("/voice/outbound-jobs")
    assert jobs_r.status_code == 200
    job_ids = [j["id"] for j in jobs_r.json()["jobs"]]
    assert job_id in job_ids


# ── Twilio webhook ─────────────────────────────────────────────────────────────

def test_twilio_webhook_xml():
    r = client.post(
        "/twilio/voice-webhook",
        json={"CallSid": "CA_webhook_test_001", "From": "+441234567890", "CallStatus": "ringing"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    body = r.text
    assert "<Response>" in body
    assert "<Say voice=\"alice\">Welcome to the travel assistance line. How can I help you today?</Say>" in body
    assert "<Gather input=\"speech\" action=\"/twilio/voice-gather\" speechTimeout=\"auto\">" in body


def test_twilio_webhook_persists_state():
    call_sid = "CA_persist_test_002"
    r = client.post(
        "/twilio/voice-webhook",
        json={"CallSid": call_sid, "CallStatus": "in-progress"},
    )
    assert r.status_code == 200

    session_r = client.get(f"/voice/session/{call_sid}")
    assert session_r.status_code == 200
    assert session_r.json()["session"]["status"] == "in-progress"


def test_twilio_voice_gather_xml():
    r = client.post(
        "/twilio/voice-gather",
        json={"CallSid": "CA_gather_test_004", "SpeechResult": "I need a flight to Madrid"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "Thanks — I’ve captured that" in r.text


def test_twilio_status_callback():
    call_sid = "CA_status_test_003"
    # First create session via webhook
    client.post("/twilio/voice-webhook", json={"CallSid": call_sid, "CallStatus": "in-progress"})

    # Then update via status callback
    r = client.post(
        "/twilio/status-callback",
        json={"CallSid": call_sid, "CallStatus": "completed"},
    )
    assert r.status_code == 200
    assert r.json()["call_status"] == "completed"

    # Verify state was updated
    session_r = client.get(f"/voice/session/{call_sid}")
    assert session_r.json()["session"]["status"] == "completed"


# ── Back-office (demo mode) ───────────────────────────────────────────────────

def test_crm_sync_demo():
    r = client.post(
        "/backoffice/crm-sync",
        json={"customer_name": "Test", "departure": "LHR", "destination": "DXB"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["mode"] == "demo"


def test_reservation_handoff_demo():
    r = client.post(
        "/backoffice/reservation-handoff",
        json={"booking_reference": "ABC123", "agent": "travel-ai"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "handoff_create"
    assert body["result"]["mode"] == "demo"


# ── Reminder ──────────────────────────────────────────────────────────────────

def test_reminder_sms():
    r = client.post(
        "/voice/reminder",
        json={"customer_name": "Carol", "channel": "sms", "trip_summary": "LHR→NYC on Apr 15"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "Twilio"
    assert "Carol" in body["message"]


def test_reminder_email():
    r = client.post(
        "/voice/reminder",
        json={"customer_name": "Dave", "channel": "email", "trip_summary": "Paris trip"},
    )
    assert r.status_code == 200
    assert r.json()["provider"] == "Email"


# ── Session not found ─────────────────────────────────────────────────────────

def test_session_not_found():
    r = client.get("/voice/session/CA_nonexistent_xyz")
    assert r.status_code == 404


# ── Input validation ──────────────────────────────────────────────────────────

def test_inbound_requires_departure():
    r = client.post(
        "/voice/inbound",
        json={"destination": "DXB", "travel_date": "2026-04-15", "passengers": 1},
    )
    assert r.status_code == 422


def test_outbound_requires_fields():
    r = client.post("/voice/outbound", json={"customer_name": "Missing reason"})
    assert r.status_code == 422


# ── Cleanup ───────────────────────────────────────────────────────────────────

def test_cleanup():
    """Remove test DB after suite."""
    import sqlite3
    try:
        from main import _conn
        if _conn:
            _conn.close()
    except Exception:
        pass
    from pathlib import Path
    Path("test_calls.db").unlink(missing_ok=True)
