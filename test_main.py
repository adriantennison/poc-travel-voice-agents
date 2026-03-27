from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_inbound_exposes_backoffice_handoff_surfaces():
    response = client.post(
        '/voice/inbound',
        json={
            'departure': 'LHR',
            'destination': 'DXB',
            'travel_date': '2026-04-15',
            'passengers': 2,
            'budget': 1500,
            'cabin': 'premium',
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body['provider'] == 'Twilio'
    assert body['crm_sync']['endpoint'] == '/backoffice/crm-sync'
    assert body['reservation_handoff']['endpoint'] == '/backoffice/reservation-handoff'


def test_backoffice_reservation_handoff_accepts_payload():
    response = client.post(
        '/backoffice/reservation-handoff',
        json={'booking_reference': 'ABC123', 'agent': 'travel-ai'}
    )
    assert response.status_code == 200
    assert response.json()['action'] == 'handoff_create'
