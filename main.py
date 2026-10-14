"""Employee Attendance & Analytics API.

Run with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (environment wins over .env).
"""
from __future__ import annotations

from bson import json_util
import json

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Existing environment values must not be overridden by .env values.
load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000) if MONGO_URI else MongoClient(serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 410_244_480_0000
EMP_RE = re.compile(r"^EMP\d{4,6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    """Idempotent indexes used by operational endpoints and common analytics filters."""
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)], name="ix_attendance_employee_punchin")


def err(status: int, detail: str):
    raise HTTPException(status_code=status, detail=detail)


def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        err(422, f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        err(422, f"{field} must be a valid date")


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        err(422, "month must be YYYY-MM")
    try:
        y, m = map(int, value.split("-"))
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        return first, last
    except ValueError:
        err(422, "month must be a valid YYYY-MM value")


def parse_epoch_ms(value: Any, field: str = "timestamp") -> datetime:
    # bool is an int subclass; explicitly reject it.
    if isinstance(value, bool) or not isinstance(value, int):
        err(422, f"{field} must be an integer epoch-milliseconds value")
    if value < EPOCH_MIN or value > EPOCH_MAX:
        err(422, f"{field} must be epoch milliseconds in the supported range")
    try:
        dt = datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        err(422, f"{field} is out of range")
    return dt


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # PyMongo normally returns naive UTC datetimes unless tz_aware is enabled.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch_ms(value: datetime | None) -> int | None:
    value = utc_aware(value)
    return int(value.timestamp() * 1000) if value else None


def to_ist(value: datetime) -> datetime:
    return utc_aware(value).astimezone(IST)


def shift_bounds(record_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    d = parse_date(record_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = datetime.combine(d, time(sh, sm), tzinfo=IST)
    end_date = d + timedelta(days=1) if shift_end <= shift_start else d
    end = datetime.combine(end_date, time(eh, em), tzinfo=IST)
    return start, end


def record_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = to_ist(punch_in)
    # Only overnight shifts can attribute an early-morning punch to the previous day.
    if shift_end <= shift_start:
        end_time = time(*map(int, shift_end.split(":")))
        if local.time().replace(tzinfo=None) < end_time:
            return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str) -> int:
    start, _ = shift_bounds(record_date, shift_start, "23:59" if False else shift_start)
    # shift start is the local start of the attendance record date.
    delta = (utc_aware(punch_in) - start.astimezone(UTC)).total_seconds()
    elapsed = int(delta // 60)
    return elapsed if delta > 600 else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, record_date: str) -> int:
    _, boundary = shift_bounds(record_date, shift_start, shift_end)
    delta = (utc_aware(punch_out) - boundary.astimezone(UTC)).total_seconds()
    minutes = int(delta // 60)
    return minutes if minutes >= 30 else 0


def compute_derived(punch_in: datetime | None, punch_out: datetime | None, status: str,
                    shift_start: str, shift_end: str, record_date: str) -> dict[str, Any]:
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, shift_start, record_date)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = (utc_aware(punch_out) - utc_aware(punch_in)).total_seconds()
    work_hours = round_half_up(seconds / 3600, 2)
    overtime = compute_overtime(punch_out, shift_start, shift_end, record_date)
    return {"work_hours": work_hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": work_hours < 4.50}


def public_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def public_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("id", None)
    for field in ("punch_in", "punch_out"):
        doc[field] = epoch_ms(doc.get(field))
    hist = []
    for item in doc.get("history", []):
        h = dict(item)
        changes = {}
        for k, v in h.get("changes", {}).items():
            changes[k] = epoch_ms(v) if k in {"punch_in", "punch_out"} and v is not None else v
        h["changes"] = changes
        h["at"] = epoch_ms(h.get("at"))
        hist.append(h)
    doc["history"] = hist
    return doc


def validate_page(page: int, page_size: int) -> None:
    if page < 1:
        err(422, "page must be at least 1")
    if page_size < 1 or page_size > 100:
        err(422, "page_size must be between 1 and 100")


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeIn(APIModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, v):
        if not EMP_RE.fullmatch(v):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return v

    @field_validator("email")
    @classmethod
    def valid_email(cls, v):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v):
            raise ValueError("email must be a valid email address")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_time(cls, v):
        if not TIME_RE.fullmatch(v):
            raise ValueError("time must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def distinct_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(APIModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, v):
        if v is not None:
            parse_epoch_ms(v, "punched_at")
        return v


class PunchOutIn(APIModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, v):
        if v is not None:
            parse_epoch_ms(v, "punched_at")
        return v


class RegularizeIn(APIModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_times(cls, v):
        if v is not None:
            parse_epoch_ms(v, "punch timestamp")
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        err(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        err(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1), page_size: int = Query(20)):
    validate_page(page, page_size)
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = [public_employee(x) for x in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        err(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at, "punched_at") if body.punched_at is not None else now_utc()
    d = record_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    derived = compute_derived(ts, None, body.status, emp["shift_start"], emp["shift_end"], d)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts,
           "punch_out": None, **derived, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        err(409, "attendance record already exists for this employee and date")
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        err(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at, "punched_at") if body.punched_at is not None else now_utc()
    # A punch-out can close a current-day or overnight shift. Select the latest eligible open record.
    record = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None}, sort=[("punch_in", DESCENDING)])
    if record is None:
        already = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": {"$ne": None}}, sort=[("punch_in", DESCENDING)])
        if already:
            err(409, "attendance record is already punched out")
        err(404, "no eligible attendance record found")
    pin = utc_aware(record["punch_in"])
    if ts <= pin or ts > pin + timedelta(hours=24):
        err(422, "punch-out must be after punch-in and within 24 hours")
    derived = compute_derived(pin, ts, record["status"], emp["shift_start"], emp["shift_end"], record["date"])
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        err(409, "attendance record was punched out concurrently")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1), page_size: int = Query(20)):
    validate_page(page, page_size)
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
        q.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        parse_date(date_to, "date_to")
        q.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        err(422, "date_from must be on or before date_to")
    if status is not None:
        if status not in STATUSES:
            err(422, "invalid status")
        q["status"] = status
    total = attendance.count_documents(q)
    docs = attendance.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_attendance(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{record_date}")
def regularize_attendance(emp_code: str, record_date: str, body: RegularizeIn):
    parse_date(record_date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        err(404, "employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": record_date})
    if not old:
        err(404, "attendance record not found")
    requested = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    new_status = requested.get("status", old["status"])
    if new_status in {"ABSENT", "LEAVE"} and ("punch_in" in requested or "punch_out" in requested):
        err(422, "punch times cannot be supplied for ABSENT or LEAVE")
    new_pin = parse_epoch_ms(requested["punch_in"], "punch_in") if requested.get("punch_in") is not None else old.get("punch_in")
    new_pout = parse_epoch_ms(requested["punch_out"], "punch_out") if requested.get("punch_out") is not None else old.get("punch_out")
    if new_status in {"ABSENT", "LEAVE"}:
        new_pin = new_pout = None
    else:
        if new_pin is None:
            err(422, "presence statuses require punch_in")
        if record_date_for_punch(new_pin, emp["shift_start"], emp["shift_end"]) != record_date:
            err(422, "punch_in does not belong to the specified attendance date")
        if new_pout is not None and (new_pout <= new_pin or new_pout > new_pin + timedelta(hours=24)):
            err(422, "punch_out must be after punch_in and within 24 hours")
    actual: dict[str, Any] = {}
    for key, val in (("status", new_status), ("punch_in", new_pin), ("punch_out", new_pout)):
        if old.get(key) != val:
            actual[key] = val
    if not actual:
        err(422, "regularization does not change any fields")
    derived = compute_derived(new_pin, new_pout, new_status, emp["shift_start"], emp["shift_end"], record_date)
    set_fields = {**actual, **derived}
    history_changes = {k: v for k, v in actual.items()}
    history_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": history_changes}
    # Compare the fields being edited to avoid overwriting a concurrent regularization/history update.
    query = {"_id": old["_id"], "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    updated = attendance.find_one_and_update(query, {"$set": set_fields, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        err(409, "attendance record changed concurrently; retry regularization")
    return public_attendance(updated)


def month_expr(field: str) -> dict:
    return {"$substrBytes": [field, 0, 7]}


def weekday_count(start: date, end: date) -> int:
    if end < start:
        return 0
    days = (end - start).days + 1
    weeks, rem = divmod(days, 7)
    count = weeks * 5
    for i in range(rem):
        if (start.weekday() + i) % 7 < 5:
            count += 1
    return count


def monthly_attendance_pipeline(emp_code: str, month: str) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month + "-01", "$lte": month + "-31"}}},
        {"$addFields": {"_weekday": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                         "_presence": {"$in": ["$status", list(PRESENCE)]}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": ["$_weekday", "$_presence"]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = parse_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        err(404, "employee not found")
    effective_start = max(first, parse_date(emp["joined_on"], "joined_on"))
    working_days = weekday_count(effective_start, last)
    # The pipeline groups stored derived fields; date filtering remains index-friendly.
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$addFields": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}
    ]
    agg = list(attendance.aggregate(pipeline))
    a = agg[0] if agg else {}
    present = float(a.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": round_half_up(present), "leave_days": int(a.get("leave_days", 0)),
            "late_count": int(a.get("late_count", 0)), "total_late_minutes": int(a.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(a.get("total_overtime_minutes", 0)),
            "attendance_pct": round_half_up(present / working_days * 100) if working_days else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    first, last = parse_month(month)
    # Start with employees so employees without attendance still count toward headcount.
    match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_dow": {"$cond": [{"$ifNull": ["$logs.date", False]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, None] } }},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}, {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            "work_hours_values": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$let": {"vars": {"vals": {"$filter": {"input": "$work_hours_values", "as": "v", "cond": {"$ne": ["$$v", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$vals"}, 0]}, {"$avg": "$$vals"}, None]}}},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    rows = list(employees.aggregate(pipeline))
    return {"month": month, "items": [{**r, "present_days": round_half_up(r.get("present_days", 0)),
            "avg_work_hours": round_half_up(r["avg_work_hours"]) if r.get("avg_work_hours") is not None else None} for r in rows if r.get("headcount", 0) > 0]}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "rank": 1}}
    ]
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        err(422, "to must be on or after from")
    if (end - start).days > 91:
        err(422, "date range cannot exceed 92 days")
    if not employees.find_one({"department": department}):
        err(404, "department not found")
    # Generate every date inside MongoDB so dates with no logs still appear.
    days = (end - start).days + 1
    pipeline = [
        {"$documents": [{}]},
        {"$set": {"_dates": {"$map": {"input": {"$range": [0, days]}, "as": "i",
            "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$_dates"},
        {"$set": {"date": {"$dateToString": {"date": "$_dates", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"}], "as": "_headcount"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}, "$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_emp"}},
            {"$unwind": "$_emp"}, {"$match": {"_emp.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}}], "as": "_counts"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$_counts.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_counts.late_count", 0]}, 0]},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_dates"}, 2]}, {"$lte": [{"$dayOfWeek": "$_dates"}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$round": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "headcount": 1, "is_working_day": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
        {"$sort": {"date": 1}}
    ]
    try:
        rows = list(db.aggregate(pipeline))
    except PyMongoError as exc:
        err(500, f"trend aggregation failed: {exc.__class__.__name__}")
    return {"department": department, "from": from_date, "to": to_date, "items": rows}

# Explain endpoint: expose executionStats for the same core query shapes used by handlers.
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, page: int = Query(1), page_size: int = Query(20),
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    validate_page(page, page_size)
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from:
            parse_date(date_from, "date_from"); q.setdefault("date", {})["$gte"] = date_from
        if date_to:
            parse_date(date_to, "date_to"); q.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to: err(422, "date_from must be on or before date_to")
        if status: q["status"] = status
        explain = attendance.database.command(
    "explain",
    {
        "find": attendance.name,
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size
    },
    verbosity="executionStats"
    ) 
        return json.loads(json_util.dumps({
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }))
    if endpoint == "employee_monthly":
        if not emp_code or not month: err(422, "emp_code and month are required")
        parse_month(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": month + "-01", "$lte": month + "-31"}}}, {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        explain = attendance.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return json.loads(json_util.dumps({
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }))

    if endpoint == "department_summary":
        if not month:
            err(422, "month is required")
        
        first, last = parse_month(month)
        
        match: dict[str, Any] = {
            "joined_on": {"$lte": last.isoformat()}
        }
        
        if department is not None:
            match["department"] = department
        
        pipeline = [
            {"$match": match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", first.isoformat()]},
                                        {"$lte": ["$date", last.isoformat()]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True
                }
            },
            {
                "$addFields": {
                    "_dow": {
                        "$cond": [
                            {"$ifNull": ["$logs.date", False]},
                            {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$logs.date"
                                    }
                                }
                            },
                            None
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {"$gte": ["$_dow", 2]},
                                        {"$lte": ["$_dow", 6]}
                                    ]
                                },
                                {"$cond": ["$logs.half_day", 0.5, 1]},
                                0
                            ]
                        }
                    },
                    "work_hours_values": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {"$ne": ["$logs.work_hours", None]}
                                    ]
                                },
                                "$logs.work_hours",
                                None
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$logs.late_minutes", 0]},
                                1, 0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1, 0
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1, 0
                            ]
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$headcount"},
                    "present_days": 1,
                    "avg_work_hours": {
                        "$let": {
                            "vars": {
                                "vals": {
                                    "$filter": {
                                        "input": "$work_hours_values",
                                        "as": "v",
                                        "cond": {"$ne": ["$$v", None]}
                                    }
                                }
                            },
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$size": "$$vals"}, 0]},
                                    {"$avg": "$$vals"},
                                    None
                                ]
                            }
                        }
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1
                }
            },
            {"$sort": {"department": 1}}
        ]
        
        explain = employees.database.command(
            "explain",
            {
                "aggregate": employees.name,
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        
        return json.loads(json_util.dumps({
            "endpoint": endpoint,
            "collection": employees.name,
            "explain": explain
        }))

    
    if endpoint == "late_leaderboard":
        print("DEBUG: late_leaderboard branch reached")
        if not month:
            err(422, "month is required")

        first, last = parse_month(month)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat()
                    },
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee"
                }
            },
            {"$unwind": "$employee"}
        ]

        if department is not None:
            pipeline.append({
                "$match": {"employee.department": department}
            })

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}}
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1
                }
            }
        ])

        explain = attendance.database.command(
            "explain",
            {
                "aggregate": attendance.name,
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        
        return json.loads(json_util.dumps({
            "endpoint": endpoint,
            "collection": attendance.name,
            "explain": explain
        }))


    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            err(422, "department, from, and to are required")
        
        parse_date(from_date, "from")
        parse_date(to_date, "to")

        if to_date < from_date:
            err(422, "invalid date range")

        if (parse_date(to_date) - parse_date(from_date)).days > 91:
            err(422, "invalid date range")

        pipeline = [
            {
                "$match": {
                    "department": department,
                    "date": {
                        "$gte": from_date,
                        "$lte": to_date
                    }
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "total_records": {"$sum": 1}
                }
            },
            {
                "$sort": {"_id": 1}
            }
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": attendance.name,
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        return json.loads(json_util.dumps({
            "endpoint": endpoint,
            "collection": attendance.name,
            "explain": explain
        }))