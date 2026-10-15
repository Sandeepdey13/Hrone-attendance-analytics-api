from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app, compute_work_hours, parse_date, parse_month

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_parse_date_valid():
    assert parse_date("2026-10-15").isoformat() == "2026-10-15"


def test_parse_date_invalid():
    with pytest.raises(HTTPException) as error:
        parse_date("2026-02-30")
    assert error.value.status_code == 422


def test_parse_month_valid():
    assert parse_month("2024-02") == ("2024-02-01", "2024-02-29")


def test_parse_month_invalid():
    with pytest.raises(HTTPException) as error:
        parse_month("2026-13")
    assert error.value.status_code == 422


def test_compute_work_hours():
    start = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 15, 17, 30, tzinfo=timezone.utc)
    assert compute_work_hours(start, end) == 8.5


def test_employee_validation_rejects_invalid_code():
    response = client.post(
        "/employees",
        json={
            "emp_code": "BAD",
            "name": "Test Employee",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    assert response.status_code == 422


def test_employee_validation_rejects_invalid_email():
    response = client.post(
        "/employees",
        json={
            "emp_code": "EMP9999",
            "name": "Test Employee",
            "email": "not-an-email",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        },
    )
    assert response.status_code == 422


def test_attendance_rejects_invalid_status():
    response = client.get("/attendance", params={"status": "UNKNOWN"})
    assert response.status_code == 422


def test_attendance_rejects_invalid_date_range():
    response = client.get(
        "/attendance",
        params={"date_from": "2026-10-20", "date_to": "2026-10-10"},
    )
    assert response.status_code == 422
