import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(settings, scheduler):
    return TestClient(create_app(settings, scheduler=scheduler))


def test_web_booking_flow(client):
    days = client.get("/api/availability", params={"start": "2026-10-13", "days": 2}).json()
    assert days[0]["slots"][0] == "09:00"

    body = {"patient_name": "Amina", "phone": "0612345678", "start": "2026-10-13T09:00", "reason": "fièvre"}
    r = client.post("/api/appointments", json=body)
    assert r.status_code == 201, r.text
    appt_id = r.json()["id"]

    assert client.post("/api/appointments", json=body).status_code == 409
    days = client.get("/api/availability", params={"start": "2026-10-13", "days": 1}).json()
    assert "09:00" not in days[0]["slots"]

    assert client.get("/api/appointments", params={"phone": "0612345678"}).json()[0]["id"] == appt_id
    assert client.post(f"/api/appointments/{appt_id}/cancel", json={"phone": "0699999999"}).status_code == 404
    assert client.post(f"/api/appointments/{appt_id}/cancel", json={"phone": "0612345678"}).status_code == 200


def test_invalid_slot_is_422(client):
    r = client.post("/api/appointments", json={"patient_name": "Amina", "phone": "0612345678", "start": "2026-10-13T13:00"})
    assert r.status_code == 422


def test_admin_requires_token(client, monkeypatch):
    assert client.get("/api/admin/appointments", params={"day": "2026-10-13"}).status_code == 401
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    r = client.get("/api/admin/appointments", params={"day": "2026-10-13"}, headers={"X-Admin-Token": "secret"})
    assert r.status_code == 200


def test_pages_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/call").status_code == 200
