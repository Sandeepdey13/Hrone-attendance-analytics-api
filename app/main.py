import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Iterable, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MILLIS = 100_000_000_000
MAX_MILLIS = 4_102_444_800_000
GRACE_SECONDS = 600
MIN_OVERTIME_SECONDS = 1800
MAX_SHIFT_SECONDS = 24 * 60 * 60
HALF_DAY_BELOW = 4.50
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
CLEARED_STATUSES = ("ABSENT", "LEAVE")

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Millis = Annotated[StrictInt, Field(ge=MIN_MILLIS, le=MAX_MILLIS)]
ExplainTarget = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]

client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]

INDEXES = [
    ("employees", "emp_code", [("emp_code", ASCENDING)], True),
    ("employees", "joined_on", [("joined_on", ASCENDING)], False),
    ("employees", "department_joined_on", [("department", ASCENDING), ("joined_on", ASCENDING)], False),
    ("employees", "department_emp_code", [("department", ASCENDING), ("emp_code", ASCENDING)], False),
    ("attendance_logs", "emp_code_date", [("emp_code", ASCENDING), ("date", ASCENDING)], True),
    ("attendance_logs", "date_emp_code", [("date", DESCENDING), ("emp_code", ASCENDING)], False),
    ("attendance_logs", "emp_code_punch_in", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], False),
    ("attendance_logs", "date_late_minutes", [("date", ASCENDING), ("late_minutes", DESCENDING)], False),
    (
        "attendance_logs",
        "status_date_emp_code",
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        False,
    ),
]

available_indexes: set[str] = set()


def setup_indexes() -> None:
    for collection, name, keys, unique in INDEXES:
        db[collection].create_index(keys, name=name, unique=unique)
        available_indexes.add(name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def hint(name: str) -> Optional[str]:
    return name if name in available_indexes else None


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def to_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int((as_utc(value) - EPOCH).total_seconds() * 1000)


def from_millis(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return as_utc(EPOCH + timedelta(milliseconds=value))


def now() -> datetime:
    return as_utc(datetime.now(UTC))


def looks_like_day(value: Any) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except (AttributeError, TypeError, ValueError):
        return False


def parse_day(value: str, field: str) -> date:
    if not looks_like_day(value):
        raise HTTPException(422, f"{field} must be a date in YYYY-MM-DD format")
    return date.fromisoformat(value)


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be in YYYY-MM format")
    year, month = (int(part) for part in value.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def require(value: Any, message: str) -> Any:
    if value is None:
        raise HTTPException(422, message)
    return value


def round_half_up(value: Any, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def ist_instant(day: date, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hour, minute), IST).astimezone(UTC)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < time.fromisoformat(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    elapsed = (punch_in - ist_instant(day, shift_start)).total_seconds()
    return int(elapsed // 60) if elapsed > GRACE_SECONDS else 0


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, day: date) -> int:
    closes_on = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    elapsed = (punch_out - ist_instant(closes_on, shift_end)).total_seconds()
    return int(elapsed // 60) if elapsed >= MIN_OVERTIME_SECONDS else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def weekday_count(first: date, last: date) -> int:
    if first > last:
        return 0
    weeks, spare = divmod((last - first).days + 1, 7)
    trailing = sum(1 for step in range(spare) if (first.weekday() + step) % 7 < 5)
    return weeks * 5 + trailing


def derive(
    employee: dict,
    day: date,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict:
    if status in CLEARED_STATUSES:
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    hours = work_hours(punch_in, punch_out) if punch_out else None
    return {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": hours,
        "late_minutes": late_minutes(punch_in, employee["shift_start"], day),
        "overtime_minutes": (
            overtime_minutes(punch_out, employee["shift_start"], employee["shift_end"], day) if punch_out else 0
        ),
        "half_day": hours < HALF_DAY_BELOW if hours is not None else False,
    }


def stored_values(record: dict) -> dict:
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    return {
        "status": record["status"],
        "punch_in": as_utc(punch_in) if punch_in else None,
        "punch_out": as_utc(punch_out) if punch_out else None,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }


def employee_json(document: dict) -> dict:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": to_millis(document["created_at"]),
    }


def change_json(field: str, change: dict) -> dict:
    if field in ("punch_in", "punch_out"):
        return {"from": to_millis(change.get("from")), "to": to_millis(change.get("to"))}
    return {"from": change.get("from"), "to": change.get("to")}


def history_json(entries: Optional[list]) -> list:
    return [
        {
            "at": to_millis(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": {field: change_json(field, change) for field, change in (entry.get("changes") or {}).items()},
        }
        for entry in entries or []
    ]


def record_json(record: dict) -> dict:
    values = stored_values(record)
    return {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": values["status"],
        "punch_in": to_millis(values["punch_in"]),
        "punch_out": to_millis(values["punch_out"]),
        "work_hours": values["work_hours"],
        "late_minutes": values["late_minutes"],
        "overtime_minutes": values["overtime_minutes"],
        "half_day": values["half_day"],
        "history": history_json(record.get("history")),
    }


class StrictBody(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("null is not accepted, omit the field instead")
        return value


class EmployeeBody(StrictBody):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value: str) -> str:
        if not looks_like_day(value):
            raise ValueError("joined_on must be a date in YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def check_shift(self) -> "EmployeeBody":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInBody(StrictBody):
    emp_code: str
    punched_at: Optional[Millis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutBody(StrictBody):
    emp_code: str
    punched_at: Optional[Millis] = None


class RegularizeBody(StrictBody):
    status: Optional[Status] = None
    punch_in: Optional[Millis] = None
    punch_out: Optional[Millis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


WEEKDAY_STAGE = {
    "$set": {"is_weekday": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}}
}
DAY_WEIGHT = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
LATE_COUNT = {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
LATE_MINUTES_TOTAL = {"$sum": {"$ifNull": ["$late_minutes", 0]}}
OVERTIME_TOTAL = {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
COUNTED_WORK_HOURS = {"$and": [{"$in": ["$status", PRESENT_STATUSES]}, {"$gt": ["$work_hours", None]}]}


def present_total(weekdays_only: bool) -> dict:
    conditions: list = [{"$in": ["$status", PRESENT_STATUSES]}]
    if weekdays_only:
        conditions.append("$is_weekday")
    return {"$sum": {"$cond": [{"$and": conditions}, DAY_WEIGHT, 0]}}


def status_total(status: str) -> dict:
    return {"$sum": {"$cond": [{"$eq": ["$status", status]}, 1, 0]}}


def half_up_expr(value: Any, places: int) -> dict:
    scale = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value, scale]}, 0.5]}}, scale]}


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    first = parse_day(date_from, "date_from") if date_from is not None else None
    last = parse_day(date_to, "date_to") if date_to is not None else None
    if first and last and first > last:
        raise HTTPException(422, "date_from must not be later than date_to")
    if first or last:
        bounds = {}
        if first:
            bounds["$gte"] = first.isoformat()
        if last:
            bounds["$lte"] = last.isoformat()
        query["date"] = bounds
    return query


def attendance_hint(query: dict) -> Optional[str]:
    if "emp_code" in query:
        return hint("emp_code_date")
    if "status" in query:
        return hint("status_date_emp_code")
    return hint("date_emp_code")


def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        WEEKDAY_STAGE,
        {
            "$group": {
                "_id": None,
                "present_days": present_total(True),
                "leave_days": status_total("LEAVE"),
                "late_count": LATE_COUNT,
                "total_late_minutes": LATE_MINUTES_TOTAL,
                "total_overtime_minutes": OVERTIME_TOTAL,
            }
        },
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    WEEKDAY_STAGE,
                    {
                        "$group": {
                            "_id": None,
                            "present_days": present_total(True),
                            "work_hours_sum": {"$sum": {"$cond": [COUNTED_WORK_HOURS, "$work_hours", 0]}},
                            "work_hours_records": {"$sum": {"$cond": [COUNTED_WORK_HOURS, 1, 0]}},
                            "late_count": LATE_COUNT,
                            "total_late_minutes": LATE_MINUTES_TOTAL,
                            "leave_count": status_total("LEAVE"),
                            "on_duty_count": status_total("ON_DUTY"),
                        }
                    },
                ],
                "as": "totals",
            }
        },
        {"$set": {"totals": {"$first": "$totals"}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$totals.present_days", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$totals.work_hours_sum", 0]}},
                "work_hours_records": {"$sum": {"$ifNull": ["$totals.work_hours_records", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$totals.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$totals.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$totals.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$totals.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_records", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_records"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_pipeline(department: str, first: date, last: date) -> list:
    lower = datetime.combine(first, time.min, UTC)
    upper = datetime.combine(last + timedelta(days=1), time.min, UTC)
    return [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": present_total(False), "late_count": LATE_COUNT}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "total"},
                ],
                "as": "staff",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$first": "$staff.total"}, 0]}}},
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def find_employee(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


def first_row(rows: Iterable[dict]) -> dict:
    return next(iter(rows), {})


def explain(command: dict, index: Optional[str] = None) -> dict:
    if index:
        command = {**command, "hint": index}
    return db.command("explain", command, verbosity="executionStats")


def explain_pipeline(collection: str, pipeline: list, index: Optional[str] = None) -> dict:
    return explain({"aggregate": collection, "pipeline": pipeline, "cursor": {}}, index)


@app.get("/health")
def health() -> dict:
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeBody) -> dict:
    document = body.model_dump()
    document["created_at"] = now()
    try:
        db.employees.insert_one(dict(document))
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    query = {"department": department} if department is not None else {}
    rows = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_json(row) for row in rows],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInBody) -> dict:
    employee = find_employee(body.emp_code)
    punched_at = from_millis(body.punched_at) if body.punched_at is not None else now()
    day = attendance_day(punched_at, employee["shift_start"], employee["shift_end"])
    record = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        **derive(employee, day, body.status, punched_at, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(dict(record))
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_json(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutBody) -> dict:
    employee = find_employee(body.emp_code)
    punched_at = from_millis(body.punched_at) if body.punched_at is not None else now()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "this record is already punched out")
    opened_at = as_utc(record["punch_in"])
    worked = (punched_at - opened_at).total_seconds()
    if worked <= 0 or worked > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    values = derive(employee, date.fromisoformat(record["date"]), record["status"], opened_at, punched_at)
    closed = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": values})
    if closed.modified_count != 1:
        raise HTTPException(409, "this record is already punched out")
    record.update(values)
    return record_json(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    query = attendance_filter(emp_code, date_from, date_to, status)
    rows = (
        db.attendance_logs.find(query)
        .hint(attendance_hint(query))
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_json(row) for row in rows],
        "total": db.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeBody) -> dict:
    day = parse_day(date, "date")
    employee = find_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    current = stored_values(record)
    sent = body.model_fields_set
    status = body.status if "status" in sent else current["status"]

    if status in CLEARED_STATUSES:
        if "punch_in" in sent or "punch_out" in sent:
            raise HTTPException(422, "ABSENT and LEAVE cannot carry punch times")
        opened_at = None
        closed_at = None
    else:
        opened_at = from_millis(body.punch_in) if "punch_in" in sent else current["punch_in"]
        closed_at = from_millis(body.punch_out) if "punch_out" in sent else current["punch_out"]
        if opened_at is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if attendance_day(opened_at, employee["shift_start"], employee["shift_end"]) != day:
            raise HTTPException(422, "punch_in must stay on the attendance date of the record")
        if closed_at is not None:
            worked = (closed_at - opened_at).total_seconds()
            if worked <= 0 or worked > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    values = derive(employee, day, status, opened_at, closed_at)
    changes = {
        field: {"from": current[field], "to": value} for field, value in values.items() if current[field] != value
    }
    if not changes:
        raise HTTPException(422, "the request does not change the record")

    history = record.get("history") or []
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    applied = db.attendance_logs.update_one(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history)]}},
        {"$set": values, "$push": {"history": entry}},
    )
    if applied.modified_count != 1:
        raise HTTPException(409, "the record was changed by another request")
    record.update(values)
    record["history"] = history + [entry]
    return record_json(record)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict:
    employee = find_employee(emp_code)
    first, last = parse_month(month)
    totals = first_row(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = weekday_count(max(first, joined_on), last)
    present_days = round_half_up(totals.get("present_days", 0), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present_days / working_days * 100, 4) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict:
    first, last = parse_month(month)
    index = hint("department_joined_on") if department is not None else hint("joined_on")
    rows = db.employees.aggregate(department_summary_pipeline(first, last, department), hint=index)
    items = []
    for row in rows:
        row["present_days"] = round_half_up(row["present_days"], 2)
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict:
    first, last = parse_month(month)
    pipeline = leaderboard_pipeline(first, last, limit, department)
    rows = db.attendance_logs.aggregate(pipeline, hint=hint("date_late_minutes"))
    return {"month": month, "items": list(rows)}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: str = Query(alias="from"),
    end: str = Query(alias="to"),
) -> dict:
    first = parse_day(start, "from")
    last = parse_day(end, "to")
    if last < first:
        raise HTTPException(422, "to must not be earlier than from")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "the range must not be longer than 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    rows = db.attendance_logs.aggregate(trend_pipeline(department, first, last), hint=hint("date_emp_code"))
    items = []
    for row in rows:
        row["present_count"] = round_half_up(row["present_count"], 2)
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
        items.append(row)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    start: Optional[str] = Query(None, alias="from"),
    end: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        query = attendance_filter(emp_code, date_from, date_to, status)
        plan = explain(
            {
                "find": collection,
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            attendance_hint(query),
        )
    elif endpoint == "employee_monthly":
        collection = "attendance_logs"
        first, last = parse_month(require(month, "month is required"))
        code = require(emp_code, "emp_code is required")
        plan = explain_pipeline(collection, monthly_pipeline(code, first, last), hint("emp_code_date"))
    elif endpoint == "department_summary":
        collection = "employees"
        first, last = parse_month(require(month, "month is required"))
        index = hint("department_joined_on") if department is not None else hint("joined_on")
        plan = explain_pipeline(collection, department_summary_pipeline(first, last, department), index)
    elif endpoint == "late_leaderboard":
        collection = "attendance_logs"
        first, last = parse_month(require(month, "month is required"))
        pipeline = leaderboard_pipeline(first, last, limit, department)
        plan = explain_pipeline(collection, pipeline, hint("date_late_minutes"))
    else:
        collection = "attendance_logs"
        name = require(department, "department is required")
        first = parse_day(require(start, "from is required"), "from")
        last = parse_day(require(end, "to is required"), "to")
        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(422, "the range must cover at most 92 days and end on or after it starts")
        plan = explain_pipeline(collection, trend_pipeline(name, first, last), hint("date_emp_code"))
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(plan))}
