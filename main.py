import os
import pyodbc
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional

load_dotenv()

app = FastAPI()

SCHEMA = "EvgeniyaYankovich"

# RideStatus ENUM: 1=Scheduled, 2=InProgress, 3=Completed, 4=Cancelled
VALID_STATUSES = {1, 2, 3, 4}


def get_connection():
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={os.getenv('DB_SERVER')};"
        f"DATABASE={os.getenv('DB_DATABASE')};"
        f"UID={os.getenv('DB_USERNAME')};"
        f"PWD={os.getenv('DB_PASSWORD')};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
    )
    return pyodbc.connect(conn_str)


def create_schema_and_table():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"""
        IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = '{SCHEMA}')
        BEGIN
            EXEC('CREATE SCHEMA [{SCHEMA}]')
        END
    """)

    cursor.execute(f"""
        IF NOT EXISTS (
            SELECT * FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = '{SCHEMA}' AND TABLE_NAME = 'Rides'
        )
        BEGIN
            CREATE TABLE [{SCHEMA}].[Rides] (
                RideId UNIQUEIDENTIFIER PRIMARY KEY DEFAULT NEWID(),
                DriverId UNIQUEIDENTIFIER NOT NULL,
                Origin NVARCHAR(255) NOT NULL,
                Destination NVARCHAR(255) NOT NULL,
                DepartureTime DATETIME NOT NULL,
                SeatsAvailable INT NOT NULL,
                Status INT NOT NULL DEFAULT 1,
                CreatedAt DATETIME DEFAULT GETDATE(),
                UpdatedAt DATETIME DEFAULT GETDATE()
            )
        END
    """)

    cursor.execute(f"""
        IF NOT EXISTS (
            SELECT * FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = '{SCHEMA}' AND TABLE_NAME = 'Bookings'
        )
        BEGIN
            CREATE TABLE [{SCHEMA}].[Bookings] (
                BookingId UNIQUEIDENTIFIER PRIMARY KEY DEFAULT NEWID(),
                RideId UNIQUEIDENTIFIER NOT NULL,
                PassengerId UNIQUEIDENTIFIER NOT NULL,
                SeatsRequested INT NOT NULL,
                CreatedAt DATETIME DEFAULT GETDATE(),
                CONSTRAINT FK_Bookings_Rides FOREIGN KEY (RideId)
                    REFERENCES [{SCHEMA}].[Rides](RideId) ON DELETE CASCADE
            )
        END
    """)

    conn.commit()
    cursor.close()
    conn.close()


@app.on_event("startup")
def startup():
    create_schema_and_table()


class RideCreate(BaseModel):
    driverId: str
    origin: str
    destination: str
    departureTime: datetime
    seatsAvailable: int


class BookingCreate(BaseModel):
    passengerId: str
    seatsRequested: int


class StatusUpdate(BaseModel):
    rideStatus: int


# ---------- Rides ----------

@app.post("/rides")
def create_ride(ride: RideCreate):
    conn = get_connection()
    cursor = conn.cursor()
    new_id = cursor.execute(f"""
        INSERT INTO [{SCHEMA}].[Rides]
            (DriverId, Origin, Destination, DepartureTime, SeatsAvailable)
        OUTPUT INSERTED.RideId
        VALUES (?, ?, ?, ?, ?)
    """, ride.driverId, ride.origin, ride.destination,
        ride.departureTime, ride.seatsAvailable).fetchval()
    conn.commit()
    cursor.close()
    conn.close()
    return {"rideId": str(new_id), "action": "created"}


@app.get("/rides")
def get_rides():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM [{SCHEMA}].[Rides]")
    columns = [col[0] for col in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return rows


@app.get("/rides/{ride_id}")
def get_ride(ride_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM [{SCHEMA}].[Rides] WHERE RideId = ?", ride_id)
    row = cursor.fetchone()
    columns = [col[0] for col in cursor.description]
    cursor.close()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    return dict(zip(columns, row))


@app.delete("/rides/{ride_id}")
def delete_ride(ride_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM [{SCHEMA}].[Rides] WHERE RideId = ?", ride_id)
    deleted = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Ride not found")
    return {"rideId": ride_id, "action": "deleted"}


@app.put("/rides/{ride_id}/status")
def update_status(ride_id: str, payload: StatusUpdate):
    if payload.rideStatus not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="rideStatus must be one of 1 (Scheduled), 2 (InProgress), 3 (Completed), 4 (Cancelled)",
        )
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
        UPDATE [{SCHEMA}].[Rides]
        SET Status = ?, UpdatedAt = GETDATE()
        WHERE RideId = ?
    """, payload.rideStatus, ride_id)
    updated = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    if updated == 0:
        raise HTTPException(status_code=404, detail="Ride not found")
    return {"rideId": ride_id, "rideStatus": payload.rideStatus, "action": "updated"}


# ---------- Bookings ----------

@app.post("/rides/{ride_id}/bookings")
def create_booking(ride_id: str, booking: BookingCreate):
    if booking.seatsRequested <= 0:
        raise HTTPException(status_code=400, detail="seatsRequested must be greater than 0")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        f"SELECT SeatsAvailable FROM [{SCHEMA}].[Rides] WHERE RideId = ?", ride_id
    )
    row = cursor.fetchone()
    if row is None:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Ride not found")

    seats_available = row[0]
    if seats_available < booking.seatsRequested:
        cursor.close()
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Not enough seats available ({seats_available} left, {booking.seatsRequested} requested)",
        )

    new_booking_id = cursor.execute(f"""
        INSERT INTO [{SCHEMA}].[Bookings]
            (RideId, PassengerId, SeatsRequested)
        OUTPUT INSERTED.BookingId
        VALUES (?, ?, ?)
    """, ride_id, booking.passengerId, booking.seatsRequested).fetchval()

    cursor.execute(f"""
        UPDATE [{SCHEMA}].[Rides]
        SET SeatsAvailable = SeatsAvailable - ?, UpdatedAt = GETDATE()
        WHERE RideId = ?
    """, booking.seatsRequested, ride_id)

    conn.commit()
    cursor.close()
    conn.close()
    return {"bookingId": str(new_booking_id), "rideId": ride_id, "action": "created"}


@app.delete("/rides/{ride_id}/bookings/{booking_id}")
def delete_booking(ride_id: str, booking_id: str):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"""
        SELECT SeatsRequested FROM [{SCHEMA}].[Bookings]
        WHERE BookingId = ? AND RideId = ?
    """, booking_id, ride_id)
    row = cursor.fetchone()
    if row is None:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Booking not found")

    seats = row[0]
    cursor.execute(f"DELETE FROM [{SCHEMA}].[Bookings] WHERE BookingId = ?", booking_id)
    cursor.execute(f"""
        UPDATE [{SCHEMA}].[Rides]
        SET SeatsAvailable = SeatsAvailable + ?, UpdatedAt = GETDATE()
        WHERE RideId = ?
    """, seats, ride_id)

    conn.commit()
    cursor.close()
    conn.close()
    return {"bookingId": booking_id, "rideId": ride_id, "action": "deleted"}