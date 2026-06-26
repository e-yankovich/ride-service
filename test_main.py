import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


# ---------- fakes & fixtures ----------

class FakeCursor:
    def __init__(self, fetchval=None, fetchone=None, description=None):
        self._fetchval = fetchval
        self._fetchone = fetchone
        self.description = description or []
        self.rowcount = 0

    def execute(self, *args, **kwargs):
        return self

    def fetchval(self):
        return self._fetchval

    def fetchone(self):
        return self._fetchone

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def fake_db(monkeypatch):
    def install(cursor):
        monkeypatch.setattr(main, "get_connection", lambda: FakeConnection(cursor))
    return install


# ---------- validate_driver ----------

def test_validate_driver_ok(monkeypatch):
    monkeypatch.setattr(main.httpx, "get",
                        lambda url: httpx.Response(200, request=httpx.Request("GET", url)))
    main.validate_driver("driver-1")  # should not raise


def test_validate_driver_not_found_raises_400(monkeypatch):
    monkeypatch.setattr(main.httpx, "get",
                        lambda url: httpx.Response(404, request=httpx.Request("GET", url)))
    with pytest.raises(HTTPException) as exc:
        main.validate_driver("missing")
    assert exc.value.status_code == 400


def test_validate_driver_service_unavailable_raises_503(monkeypatch):
    def boom(url):
        raise httpx.RequestError("connection refused")

    monkeypatch.setattr(main.httpx, "get", boom)
    with pytest.raises(HTTPException) as exc:
        main.validate_driver("driver-1")
    assert exc.value.status_code == 503


# ---------- POST /rides ----------

def test_create_ride_success(client, fake_db, monkeypatch):
    monkeypatch.setattr(main, "validate_driver", lambda driver_id: None)
    fake_db(FakeCursor(fetchval="ride-123"))

    resp = client.post("/rides", json={
        "driverId": "driver-1",
        "origin": "A",
        "destination": "B",
        "departureTime": "2026-07-01T10:00:00",
        "seatsAvailable": 3,
    })

    assert resp.status_code == 200
    assert resp.json() == {"rideId": "ride-123", "action": "created"}


# ---------- GET /rides/{id} ----------

def test_get_ride_not_found_returns_404(client, fake_db):
    fake_db(FakeCursor(fetchone=None, description=[("RideId",)]))
    resp = client.get("/rides/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ride not found"


# ---------- PUT /rides/{id}/status ----------

def test_update_status_rejects_invalid_status(client):
    resp = client.put("/rides/ride-1/status", json={"rideStatus": 99})
    assert resp.status_code == 400


def test_update_status_completed_requires_rating(client):
    resp = client.put("/rides/ride-1/status", json={"rideStatus": main.STATUS_COMPLETED})
    assert resp.status_code == 400
    assert "rating is required" in resp.json()["detail"]


@pytest.mark.parametrize("rating", [0, 6])
def test_update_status_completed_rating_out_of_range(client, rating):
    resp = client.put("/rides/ride-1/status",
                      json={"rideStatus": main.STATUS_COMPLETED, "rating": rating})
    assert resp.status_code == 400
    assert "between 1 and 5" in resp.json()["detail"]


def test_update_status_completed_sends_service_bus_message(client, fake_db, monkeypatch):
    fake_db(FakeCursor(fetchval="driver-42"))
    sent = {}

    def fake_send(ride_id, driver_id, rating, comment, passenger_id):
        sent.update(ride_id=ride_id, driver_id=driver_id, rating=rating,
                    comment=comment, passenger_id=passenger_id)

    monkeypatch.setattr(main, "send_ride_completed_message", fake_send)

    resp = client.put("/rides/ride-1/status", json={
        "rideStatus": main.STATUS_COMPLETED,
        "rating": 5,
        "comment": "great",
        "passengerId": "pass-1",
    })

    assert resp.status_code == 200
    assert resp.json()["action"] == "updated"
    assert sent == {
        "ride_id": "ride-1",
        "driver_id": "driver-42",
        "rating": 5,
        "comment": "great",
        "passenger_id": "pass-1",
    }


# ---------- POST /rides/{id}/bookings ----------

def test_create_booking_rejects_non_positive_seats(client):
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 0})
    assert resp.status_code == 400


def test_create_booking_ride_not_found_returns_404(client, fake_db):
    fake_db(FakeCursor(fetchone=None))
    resp = client.post("/rides/missing/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 1})
    assert resp.status_code == 404


def test_create_booking_rejects_when_not_enough_seats(client, fake_db):
    fake_db(FakeCursor(fetchone=(1,)))  # ride has 1 seat, 2 requested
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 2})
    assert resp.status_code == 400
    assert "Not enough seats" in resp.json()["detail"]


def test_create_booking_success(client, fake_db):
    fake_db(FakeCursor(fetchval="booking-9", fetchone=(5,)))
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 2})
    assert resp.status_code == 200
    assert resp.json() == {"bookingId": "booking-9", "rideId": "ride-1", "action": "created"}
