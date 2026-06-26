from datetime import datetime

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


# ---------- fakes & fixtures ----------

# Naive datetimes so they compare against datetime.now() in the app code.
FUTURE = datetime(2999, 1, 1, 10, 0, 0)
PAST = datetime(2000, 1, 1, 10, 0, 0)


class FakeCursor:
    def __init__(self, fetchval=None, fetchone=None, fetchall=None, description=None):
        self._fetchval = fetchval
        self._fetchone = fetchone
        self._fetchall = fetchall or []
        self.description = description or []
        self.rowcount = 0
        self.executed = []

    def execute(self, *args, **kwargs):
        sql = args[0] if args else ""
        params = args[1:]
        self.executed.append((sql, params))
        return self

    def fetchval(self):
        return self._fetchval

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

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


def executed_with(cursor, sql_fragment):
    return [(sql, params) for sql, params in cursor.executed if sql_fragment in sql]


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
        "departureTime": FUTURE.isoformat(),
        "seatsAvailable": 3,
    })

    assert resp.status_code == 200
    assert resp.json() == {"rideId": "ride-123", "action": "created"}


def test_create_ride_rejects_past_departure(client, monkeypatch):
    monkeypatch.setattr(main, "validate_driver", lambda driver_id: None)

    resp = client.post("/rides", json={
        "driverId": "driver-1",
        "origin": "A",
        "destination": "B",
        "departureTime": PAST.isoformat(),
        "seatsAvailable": 3,
    })

    assert resp.status_code == 400
    assert "past" in resp.json()["detail"]


# ---------- GET /rides ----------

def test_get_rides_returns_list(client, fake_db):
    cursor = FakeCursor(
        fetchall=[("r1", "A"), ("r2", "B")],
        description=[("RideId",), ("Origin",)],
    )
    fake_db(cursor)
    resp = client.get("/rides")
    assert resp.status_code == 200
    assert resp.json() == [
        {"RideId": "r1", "Origin": "A"},
        {"RideId": "r2", "Origin": "B"},
    ]


# ---------- GET /rides/{id} ----------

def test_get_ride_not_found_returns_404(client, fake_db):
    fake_db(FakeCursor(fetchone=None, description=[("RideId",)]))
    resp = client.get("/rides/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ride not found"


# ---------- DELETE /rides/{id} ----------

def test_delete_ride_success(client, fake_db):
    cursor = FakeCursor()
    cursor.rowcount = 1
    fake_db(cursor)
    resp = client.delete("/rides/ride-1")
    assert resp.status_code == 200
    assert resp.json()["action"] == "deleted"


def test_delete_ride_not_found(client, fake_db):
    cursor = FakeCursor()
    cursor.rowcount = 0
    fake_db(cursor)
    resp = client.delete("/rides/ride-1")
    assert resp.status_code == 404


# ---------- PUT /rides/{id}/status: input validation ----------

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


def test_update_status_ride_not_found(client, fake_db):
    fake_db(FakeCursor(fetchone=None))
    resp = client.put("/rides/missing/status", json={"rideStatus": main.STATUS_IN_PROGRESS})
    assert resp.status_code == 404


# ---------- PUT /rides/{id}/status: transition rules (B) ----------

def test_update_status_rejects_transition_from_terminal(client, fake_db):
    # Completed is terminal: cannot move back to Scheduled.
    fake_db(FakeCursor(fetchone=(main.STATUS_COMPLETED, "driver-1")))
    resp = client.put("/rides/ride-1/status", json={"rideStatus": main.STATUS_SCHEDULED})
    assert resp.status_code == 409


def test_update_status_cannot_revert_in_progress_to_scheduled(client, fake_db):
    fake_db(FakeCursor(fetchone=(main.STATUS_IN_PROGRESS, "driver-1")))
    resp = client.put("/rides/ride-1/status", json={"rideStatus": main.STATUS_SCHEDULED})
    assert resp.status_code == 409


def test_update_status_cancelled_is_terminal(client, fake_db):
    fake_db(FakeCursor(fetchone=(main.STATUS_CANCELLED, "driver-1")))
    resp = client.put("/rides/ride-1/status", json={"rideStatus": main.STATUS_IN_PROGRESS})
    assert resp.status_code == 409


# ---------- PUT /rides/{id}/status: completion + Service Bus ----------

def test_update_status_completed_sends_service_bus_message(client, fake_db, monkeypatch):
    fake_db(FakeCursor(fetchone=(main.STATUS_IN_PROGRESS, "driver-42")))
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


def test_update_status_non_completed_does_not_send_message(client, fake_db, monkeypatch):
    fake_db(FakeCursor(fetchone=(main.STATUS_SCHEDULED, "driver-1")))
    called = {"sent": False}
    monkeypatch.setattr(main, "send_ride_completed_message",
                        lambda *a, **k: called.update(sent=True))

    resp = client.put("/rides/ride-1/status", json={"rideStatus": main.STATUS_IN_PROGRESS})

    assert resp.status_code == 200
    assert called["sent"] is False


def test_update_status_completed_swallows_service_bus_failure(client, fake_db, monkeypatch):
    fake_db(FakeCursor(fetchone=(main.STATUS_IN_PROGRESS, "driver-1")))

    def boom(*a, **k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(main, "send_ride_completed_message", boom)

    resp = client.put("/rides/ride-1/status",
                      json={"rideStatus": main.STATUS_COMPLETED, "rating": 5})

    assert resp.status_code == 200
    assert resp.json()["action"] == "updated"


# ---------- PUT /rides/{id}/status: cancellation frees bookings (D) ----------

def test_update_status_cancel_deletes_bookings(client, fake_db):
    cursor = FakeCursor(fetchone=(main.STATUS_SCHEDULED, "driver-1"))
    fake_db(cursor)

    resp = client.put("/rides/ride-1/status", json={"rideStatus": main.STATUS_CANCELLED})

    assert resp.status_code == 200
    deletes = [sql for sql, _ in cursor.executed if "DELETE FROM" in sql and "Bookings" in sql]
    assert deletes, "cancelling a ride should delete its bookings"


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


@pytest.mark.parametrize("status", [
    "STATUS_IN_PROGRESS", "STATUS_COMPLETED", "STATUS_CANCELLED",
])
def test_create_booking_rejects_when_not_scheduled(client, fake_db, status):
    ride_status = getattr(main, status)
    fake_db(FakeCursor(fetchone=(5, ride_status, FUTURE)))
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 1})
    assert resp.status_code == 409
    assert "not open for booking" in resp.json()["detail"]


def test_create_booking_rejects_when_ride_departed(client, fake_db):
    fake_db(FakeCursor(fetchone=(5, main.STATUS_SCHEDULED, PAST)))
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 1})
    assert resp.status_code == 409
    assert "already departed" in resp.json()["detail"]


def test_create_booking_rejects_when_not_enough_seats(client, fake_db):
    fake_db(FakeCursor(fetchone=(1, main.STATUS_SCHEDULED, FUTURE)))  # 1 seat, 2 requested
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 2})
    assert resp.status_code == 400
    assert "Not enough seats" in resp.json()["detail"]


def test_create_booking_success(client, fake_db):
    fake_db(FakeCursor(fetchval="booking-9", fetchone=(5, main.STATUS_SCHEDULED, FUTURE)))
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 2})
    assert resp.status_code == 200
    assert resp.json() == {"bookingId": "booking-9", "rideId": "ride-1", "action": "created"}


def test_create_booking_allows_exact_seat_count(client, fake_db):
    fake_db(FakeCursor(fetchval="booking-1", fetchone=(2, main.STATUS_SCHEDULED, FUTURE)))
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 2})
    assert resp.status_code == 200


def test_create_booking_decrements_seats(client, fake_db):
    cursor = FakeCursor(fetchval="booking-1", fetchone=(5, main.STATUS_SCHEDULED, FUTURE))
    fake_db(cursor)
    resp = client.post("/rides/ride-1/bookings",
                       json={"passengerId": "pass-1", "seatsRequested": 2})
    assert resp.status_code == 200
    decrements = executed_with(cursor, "SeatsAvailable = SeatsAvailable - ?")
    assert decrements and 2 in decrements[0][1]


# ---------- DELETE /rides/{id}/bookings/{booking_id} ----------

def test_delete_booking_not_found(client, fake_db):
    fake_db(FakeCursor(fetchone=None))
    resp = client.delete("/rides/ride-1/bookings/missing")
    assert resp.status_code == 404


def test_delete_booking_success_restores_seats(client, fake_db):
    cursor = FakeCursor(fetchone=(2,))  # the booking held 2 seats
    fake_db(cursor)
    resp = client.delete("/rides/ride-1/bookings/booking-1")
    assert resp.status_code == 200
    assert resp.json()["action"] == "deleted"
    restores = executed_with(cursor, "SeatsAvailable = SeatsAvailable + ?")
    assert restores and 2 in restores[0][1]
