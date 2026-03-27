from datetime import datetime
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title='Travel Voice Agents', version='1.2.0')


class InboundCall(BaseModel):
    departure: str
    destination: str
    travel_date: str
    passengers: int = Field(ge=1)
    budget: float | None = None
    cabin: Literal['economy', 'premium', 'business'] = 'economy'


class OutboundRequest(BaseModel):
    reason: str
    customer_name: str
    booking_reference: str | None = None
    callback_window: str | None = None


class ReminderRequest(BaseModel):
    customer_name: str
    channel: Literal['sms', 'email']
    trip_summary: str


@app.get('/health')
def health():
    return {'status': 'healthy', 'service': 'travel-voice-agents', 'timestamp': datetime.utcnow().isoformat()}


@app.get('/integrations')
def integrations():
    return {
        'twilio_voice_webhook': '/twilio/voice-webhook',
        'crm_sync': '/backoffice/crm-sync',
        'reservation_handoff': '/backoffice/reservation-handoff',
    }


@app.post('/voice/inbound')
def inbound(call: InboundCall):
    multiplier = {'economy': 1.0, 'premium': 1.35, 'business': 1.9}[call.cabin]
    options = [
        {'carrier': 'SkyBridge Air', 'fare': round(640 * multiplier, 2), 'currency': 'USD'},
        {'carrier': 'North Atlantic Connect', 'fare': round(710 * multiplier, 2), 'currency': 'USD'}
    ]
    lowest = min(option['fare'] for option in options)
    return {
        'provider': 'Twilio',
        'summary': f"{call.passengers} passenger(s) from {call.departure} to {call.destination} on {call.travel_date}",
        'options': options,
        'budget_ok': call.budget is None or lowest <= call.budget,
        'handoff_required': any(o['fare'] > (call.budget or 10_000) for o in options),
        'next_step': 'read itinerary options and offer live transfer',
        'crm_sync': {'endpoint': '/backoffice/crm-sync', 'method': 'POST'},
        'reservation_handoff': {'endpoint': '/backoffice/reservation-handoff', 'method': 'POST'},
    }


@app.post('/voice/outbound')
def outbound(req: OutboundRequest):
    email_followup = {
        'subject': f"Follow-up on your travel request: {req.customer_name}",
        'body': f"We tried to reach you regarding {req.reason}. Reply with a good callback time and we will continue your booking support."
    }
    return {
        'provider': 'Twilio',
        'reason': req.reason,
        'script': f"Hello {req.customer_name}, this is your travel assistant calling about {req.reason}.",
        'callback_window': req.callback_window or 'next available slot',
        'email_followup': email_followup,
        'twilio_status_callback': '/twilio/status-callback',
    }


@app.post('/voice/reminder')
def reminder(req: ReminderRequest):
    return {
        'provider': 'Twilio' if req.channel == 'sms' else 'Email',
        'message': f"Hi {req.customer_name}, this is a reminder about {req.trip_summary}.",
        'channel': req.channel,
    }


@app.post('/twilio/voice-webhook')
def twilio_voice_webhook(payload: dict):
    return {'ok': True, 'provider': 'Twilio', 'received': payload}


@app.post('/backoffice/crm-sync')
def crm_sync(payload: dict):
    return {'ok': True, 'system': 'CRM', 'action': 'sync_contact_trip_request', 'received': payload}


@app.post('/backoffice/reservation-handoff')
def reservation_handoff(payload: dict):
    return {'ok': True, 'system': 'reservation_desk', 'action': 'handoff_create', 'received': payload}
