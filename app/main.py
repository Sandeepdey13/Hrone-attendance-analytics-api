from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.json_util import JSONOptions
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
_indexes_ready = False


def create_indexes() -> None:

    global _indexes_ready
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined"
    )
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee"
    )
    _indexes_ready = True


@app.on_event("startup")
def on_startup() -> None:

    try:
        create_indexes()
    except PyMongoError:
        logger.exception("MongoDB unavailable during index creation; indexes will need a successful startup retry")


def parse_date(value: str, field_name: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid calendar date") from None


def parse_month(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    try:
        year, month = map(int, value.split("-"))
        first = date(year, month, 1)
        next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be a valid calendar month") from None
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def parse_shift(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("shift time must be HH:MM in 24-hour format")
    h, m = map(int, value.split(":"))
    return time(h, m)


def as_utc(value: datetime) -> datetime:

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:

    return datetime.fromtimestamp(value // 1000, tz=UTC)


def now_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_utc(punch_in).astimezone(IST)
    start_t = parse_shift(shift_start)
    end_t = parse_shift(shift_end)
    day = local.date()

    if end_t <= start_t and local.time().replace(tzinfo=None) < end_t:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_instant(attendance_day: str, shift_start: str) -> datetime:
    day = parse_date(attendance_day)
    local = datetime.combine(day, parse_shift(shift_start), tzinfo=IST)
    return local.astimezone(UTC)


def shift_end_instant(attendance_day: str, shift_start: str, shift_end: str) -> datetime:
    day = parse_date(attendance_day)
    start_t, end_t = parse_shift(shift_start), parse_shift(shift_end)
    if end_t <= start_t:
        day += timedelta(days=1)
    return datetime.combine(day, end_t, tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    punched = truncate_seconds(punch_in)
    start = shift_start_instant(attendance_day, shift_start)
    elapsed = int((punched - start).total_seconds())

    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str(int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())))
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    end = shift_end_instant(attendance_day, shift_start, shift_end)
    elapsed = int((truncate_seconds(punch_out) - end).total_seconds())
    if elapsed < 1800:
        return 0
    return elapsed // 60


def round_half_up_expr(expression, places: int):

    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def response_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


def response_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            old, new = change.get("from"), change.get("to")
            if key in ("punch_in", "punch_out"):
                old, new = to_epoch_ms(old), to_epoch_ms(new)
            changes[key] = {"from": old, "to": new}
        history.append({
            "at": to_epoch_ms(entry.get("at")),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def ensure_page(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    return query


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date") from None
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        parse_shift(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an integer epoch-millisecond value")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an integer epoch-millisecond value")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_times(cls, value):
        if isinstance(value, dict):
            if "status" in value and value["status"] is None:
                raise ValueError("status must be a valid attendance status")
            for key in ("punch_in", "punch_out"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} must be an integer epoch-millisecond value")
        return value


@app.get("/health")
def health():
    global _indexes_ready
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            create_indexes()
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from None


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists") from None
    except PyMongoError:
        logger.exception("Employee creation failed")
        raise HTTPException(status_code=503, detail="database unavailable") from None
    return response_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    ensure_page(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [response_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_truncated()
    day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date") from None
    return response_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    ensure_page(page, page_size)
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [response_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_truncated()

    current = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if current is None:
        raise HTTPException(status_code=404, detail="no punch-in found at or before punched_at")
    punch_in_time = as_utc(current["punch_in"])
    elapsed = (ts - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    if current.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], current["date"])
    update = {
        "$set": {
            "punch_out": ts,
            "work_hours": hours,
            "overtime_minutes": overtime,
            "half_day": hours < 4.50,
        }
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}]},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return response_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not current:
        raise HTTPException(status_code=404, detail="attendance record not found")

    changes_requested = body.model_dump(exclude_unset=True)
    changes_requested.pop("reason", None)
    changes_requested.pop("regularized_by", None)
    old_status = current.get("status")
    new_status = changes_requested.get("status", old_status)
    absent_or_leave = new_status in ("ABSENT", "LEAVE")

    if absent_or_leave and ("punch_in" in changes_requested or "punch_out" in changes_requested):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot include punch times")

    if absent_or_leave:
        new_in = None
        new_out = None
    else:
        raw_in = changes_requested.get("punch_in", to_epoch_ms(current.get("punch_in")))
        raw_out = changes_requested.get("punch_out", to_epoch_ms(current.get("punch_out")))
        if raw_in is None:
            raise HTTPException(status_code=422, detail="a presence status requires punch_in")
        new_in = epoch_ms_to_datetime(raw_in)
        new_out = epoch_ms_to_datetime(raw_out) if raw_out is not None else None
        if attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to the record attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if absent_or_leave:
        new_late, new_hours, new_overtime, new_half_day = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"], date)
        if new_out is None:
            new_hours, new_overtime, new_half_day = None, 0, False
        else:
            new_hours = compute_work_hours(new_in, new_out)
            new_overtime = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = new_hours < 4.50

    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }
    actual_changes = {}
    for key, value in proposed.items():
        old_value = current.get(key)
        if key == "late_minutes":
            old_value = int(old_value or 0)
        elif key == "overtime_minutes":
            old_value = int(old_value or 0)
        elif key == "half_day":
            old_value = bool(old_value or False)
        if old_value != value:
            actual_changes[key] = {"from": old_value, "to": value}
    if not actual_changes:
        raise HTTPException(status_code=422, detail="regularization would not change the record")

    entry = {
        "at": now_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": actual_changes,
    }

    filter_doc = {"_id": current["_id"]}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if key in current:
            filter_doc[key] = current[key]
        else:
            filter_doc[key] = {"$exists": False}
    if "history" in current:
        filter_doc["history"] = current.get("history")
    else:
        filter_doc["history"] = {"$exists": False}

    update_fields = dict(proposed)

    updated = db.attendance_logs.find_one_and_update(
        filter_doc,
        {"$set": update_fields, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry regularization")
    return response_attendance(updated)


def weekday_expr(date_expression):
    dow = {"$dayOfWeek": {"date": date_expression, "timezone": "Asia/Kolkata"}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def presence_day_expr():
    is_present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    is_weekday = weekday_expr({"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}})
    value = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}
    return {"$cond": [{"$and": [is_present, is_weekday]}, value, 0.0]}


def employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {"__month_start": month_start, "__month_end": month_end}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code", "joined_on": "$joined_on", "start": "$__month_start", "end": "$__month_end"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", "$$start"]}, {"$lte": ["$date", "$$end"]},
                    {"$gte": ["$date", "$$joined_on"]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": presence_day_expr()},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ],
            "as": "__stats",
        }},
        {"$set": {
            "__start_str": {"$cond": [{"$gt": ["$joined_on", "$__month_start"]}, "$joined_on", "$__month_start"]},
        }},
        {"$set": {
            "__start_date": {"$dateFromString": {"dateString": "$__start_str", "timezone": "Asia/Kolkata"}},
            "__end_date": {"$dateFromString": {"dateString": "$__month_end", "timezone": "Asia/Kolkata"}},
            "__stats_doc": {"$ifNull": [{"$arrayElemAt": ["$__stats", 0]}, {
                "present_days": 0, "leave_days": 0, "late_count": 0,
                "total_late_minutes": 0, "total_overtime_minutes": 0,
            }]},
        }},
        {"$set": {
            "__day_count": {"$max": [0, {"$add": [{"$dateDiff": {
                "startDate": "$__start_date", "endDate": "$__end_date", "unit": "day", "timezone": "Asia/Kolkata"
            }}, 1]}]},
        }},
        {"$set": {
            "__working_days": {"$size": {"$filter": {
                "input": {"$map": {
                    "input": {"$range": [0, "$__day_count"]}, "as": "offset",
                    "in": {"$dateAdd": {"startDate": "$__start_date", "unit": "day", "amount": "$$offset", "timezone": "Asia/Kolkata"}},
                }},
                "as": "day", "cond": weekday_expr("$$day"),
            }}},
        }},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$substrBytes": ["$__month_start", 0, 7]},
            "working_days": "$__working_days",
            "present_days": round_half_up_expr("$__stats_doc.present_days", 2),
            "leave_days": "$__stats_doc.leave_days", "late_count": "$__stats_doc.late_count",
            "total_late_minutes": "$__stats_doc.total_late_minutes",
            "total_overtime_minutes": "$__stats_doc.total_overtime_minutes",
            "attendance_pct": {"$cond": [
                {"$gt": ["$__working_days", 0]},
                round_half_up_expr({"$multiply": [{"$divide": ["$__stats_doc.present_days", "$__working_days"]}, 100]}, 4),
                None,
            ]},
        }},
    ]


def department_summary_pipeline(month_start: str, month_end: str, department: Optional[str]) -> list[dict]:
    employee_match: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code", "joined_on": "$joined_on", "start": month_start, "end": month_end},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", "$$start"]}, {"$lte": ["$date", "$$end"]},
                    {"$gte": ["$date", "$$joined_on"]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": presence_day_expr()},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]}, 1, 0
                    ]}},
                }},
            ],
            "as": "__stats",
        }},
        {"$set": {"__stats_doc": {"$ifNull": [{"$arrayElemAt": ["$__stats", 0]}, {
            "present_days": 0, "late_count": 0, "total_late_minutes": 0,
            "leave_count": 0, "on_duty_count": 0, "work_hours_sum": 0, "work_hours_count": 0,
        }]}}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$__stats_doc.present_days"},
            "late_count": {"$sum": "$__stats_doc.late_count"},
            "total_late_minutes": {"$sum": "$__stats_doc.total_late_minutes"},
            "leave_count": {"$sum": "$__stats_doc.leave_count"},
            "on_duty_count": {"$sum": "$__stats_doc.on_duty_count"},
            "work_hours_sum": {"$sum": "$__stats_doc.work_hours_sum"},
            "work_hours_count": {"$sum": "$__stats_doc.work_hours_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": round_half_up_expr("$present_days", 2),
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                round_half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2), None,
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str]) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "let": {"employee_code": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$emp_code", "$$employee_code"]}}},
                {"$project": {"_id": 0, "name": 1, "department": 1}},
            ], "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, start_date: str, end_date: str) -> list[dict]:

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$set": {"start": start_date, "end": end_date}},
        {"$set": {
            "__start": {"$dateFromString": {"dateString": "$start", "timezone": "Asia/Kolkata"}},
            "__end": {"$dateAdd": {
                "startDate": {"$dateFromString": {"dateString": "$end", "timezone": "Asia/Kolkata"}},
                "unit": "day", "amount": 1, "timezone": "Asia/Kolkata",
            }},
        }},
        {"$set": {"__days": {"$map": {
            "input": {"$range": [0, {"$dateDiff": {"startDate": "$__start", "endDate": "$__end", "unit": "day", "timezone": "Asia/Kolkata"}}]},
            "as": "offset", "in": {"$dateAdd": {"startDate": "$__start", "unit": "day", "amount": "$$offset", "timezone": "Asia/Kolkata"}},
        }}}},
        {"$unwind": "$__days"},
        {"$set": {
            "date_value": "$__days",
            "date": {"$dateToString": {"date": "$__days", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}},
            "is_working_day": weekday_expr("$__days"),
        }},
        {"$lookup": {
            "from": "employees", "let": {"dept": "$department", "day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", "$$day"]},
                ]}}},
                {"$count": "count"},
            ], "as": "__headcount_rows",
        }},
        {"$lookup": {
            "from": "attendance_logs", "let": {"day": "$date", "dept": "$department"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees", "let": {"ec": "$emp_code"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}}},
                        {"$project": {"department": 1, "joined_on": 1}},
                    ], "as": "__employee",
                }},
                {"$unwind": "$__employee"},
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$__employee.department", "$$dept"]},
                    {"$lte": ["$__employee.joined_on", "$$day"]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ], "as": "__daily_rows",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$__headcount_rows.count", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$__daily_rows.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$__daily_rows.late_count", 0]}, 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4), None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date_value": 1},
            "output": {"__moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$ne": ["$__moving_avg", None]}, round_half_up_expr("$__moving_avg", 4), None,
        ]}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month_start, month_end = parse_month(month)
    if not db.employees.find_one({"emp_code": emp_code}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="employee not found")
    result = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month_start, month_end), allowDiskUse=True))
    if not result:
        raise HTTPException(status_code=404, detail="employee not found")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_start, month_end = parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month_start, month_end, department), allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = parse_month(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month_start, month_end, limit, department), allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    start = parse_date(from_, "from")
    end = parse_date(to, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="trend range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="department not found")
    items = list(db.employees.aggregate(department_trend_pipeline(department, start.isoformat(), end.isoformat()), allowDiskUse=True))
    return {"department": department, "items": items}


EndpointName = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _explain_command(command: dict) -> dict:
    try:
        return db.command("explain", command, verbosity="executionStats")
    except PyMongoError as exc:
        logger.exception("Explain failed")
        raise HTTPException(status_code=503, detail="MongoDB explain failed") from exc


def _extended_json(value: dict) -> dict:

    return json_util.loads(json_util.dumps(value, json_options=JSONOptions(json_mode=json_util.JSONMode.RELAXED)))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EndpointName,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    ensure_page(page, page_size)
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs", "filter": query,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        month_start, month_end = parse_month(month)
        command = {"aggregate": "employees", "pipeline": employee_monthly_pipeline(emp_code, month_start, month_end), "cursor": {}, "allowDiskUse": True}
        collection = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        month_start, month_end = parse_month(month)
        command = {"aggregate": "employees", "pipeline": department_summary_pipeline(month_start, month_end, department), "cursor": {}, "allowDiskUse": True}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        month_start, month_end = parse_month(month)
        command = {"aggregate": "attendance_logs", "pipeline": late_leaderboard_pipeline(month_start, month_end, limit, department), "cursor": {}, "allowDiskUse": True}
        collection = "attendance_logs"
    else:
        if not department or not from_ or not to:
            raise HTTPException(422, detail="department, from and to are required for department_trend")
        start, end = parse_date(from_, "from"), parse_date(to, "to")
        if end < start:
            raise HTTPException(422, detail="to must be on or after from")
        if (end - start).days + 1 > 92:
            raise HTTPException(422, detail="trend range cannot exceed 92 days")
        command = {"aggregate": "employees", "pipeline": department_trend_pipeline(department, start.isoformat(), end.isoformat()), "cursor": {}, "allowDiskUse": True}
        collection = "employees"
    explain = _explain_command(command)
    return {"endpoint": endpoint, "collection": collection, "explain": _extended_json(explain)}
