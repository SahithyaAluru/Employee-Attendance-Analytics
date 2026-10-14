"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment
      variables always win because python-dotenv never overrides existing ones)

Part A (review) is done: the starter's defects are fixed here and documented in REVIEW.md.
All contract endpoints are implemented below, including database-side analytics.

Contract conventions (openapi.yaml):
  * Instants cross the HTTP boundary as integer epoch MILLISECONDS; MongoDB stores them
    as BSON dates (UTC). Truncated to whole seconds before any calculation (R1).
  * Calendar dates are "YYYY-MM-DD" and times of day "HH:MM" strings, all IST (UTC+5:30).
  * Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed
    only by this API - never accepted from a client.
"""
import os
import calendar
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from bson import Decimal128, json_util
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # never overrides real env vars -> environment wins (contract)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,  # /health answers 503 promptly when MongoDB is down
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))  # UTC+05:30, no DST (R1)
UTC = timezone.utc

# EpochMillis bounds (openapi.yaml components/schemas/EpochMillis)
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000


def ensure_indexes() -> None:
    """Create every index the API needs. Idempotent: running twice is a no-op."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    # Natural key of a record - the race-safe punch-in guarantee.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Covers GET /attendance sort (date desc, emp_code asc) and date-range filters.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Readiness must not advertise race-safe writes without their unique indexes.
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Boundary helpers: epoch milliseconds <-> datetime.
# PyMongo returns NAIVE datetimes that actually are UTC (DATA_MODEL.md).
def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp()) * 1000


def as_utc(dt: datetime) -> datetime:
    """PyMongo hands back naive datetimes that are UTC; make that explicit."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def iso_date(value: str) -> date:
    """Strict YYYY-MM-DD parse; raises ValueError (-> 422) otherwise."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("must be a valid calendar date in YYYY-MM-DD format")
    if parsed.isoformat() != value:  # rejects non-padded forms like 2026-1-5
        raise ValueError("must be a valid calendar date in YYYY-MM-DD format")
    return parsed


def strict_calendar_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("must be a calendar date in YYYY-MM-DD format")
    return iso_date(value)


CalendarDate = Annotated[date, BeforeValidator(strict_calendar_date)]


def epoch_ms_field(value):
    """Pydantic 'before' validator: a strict int in the contract's epoch-ms range.

    An omitted field never reaches the validator (the default applies), so `None`
    here means the client explicitly sent JSON null - rejected like any non-int.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer number of milliseconds since 1970-01-01T00:00:00Z")
    if not (EPOCH_MS_MIN <= value <= EPOCH_MS_MAX):
        raise ValueError(f"must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX} (epoch milliseconds)")
    return value


def validation_error(message: str, field: str = "body") -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": (field,),
                                    "msg": message, "input": None}])


def record_snapshot(record: dict) -> dict:
    """Compare the complete mutable state and audit length before an atomic write."""
    fields = ("emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
              "late_minutes", "overtime_minutes", "half_day")
    return {**{key: record.get(key) for key in fields},
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}},
                              len(record.get("history") or [])]}}


# Business rules R1-R5 - computed only from punch times + the employee's shift.
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # R1


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: date = IST calendar date of punch_in. Overnight shifts: a punch-in whose IST
    time is earlier than shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def shift_start_dt(day: date, shift_start: str) -> datetime:
    hour, minute = map(int, shift_start.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def shift_end_dt(day: date, shift_end: str, overnight: bool) -> datetime:
    if overnight:
        day += timedelta(days=1)  # R1: overnight shift ends next calendar day
    hour, minute = map(int, shift_end.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    """R2: late only when strictly more than 10:00 after shift_start; then whole
    minutes (floor) since shift_start itself. Boundaries: 09:40:00 -> 0, 09:40:01 -> 10,
    10:15:59 -> 45."""
    delta = int((punch_in - shift_start_dt(day, shift_start)).total_seconds())
    if delta <= 10 * 60:
        return 0
    return delta // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded to 2 decimals HALF-UP (never banker's rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, day: date, overnight: bool) -> int:
    """R3: whole minutes after shift_end, only when >= 30, otherwise 0."""
    delta = int((punch_out - shift_end_dt(day, shift_end, overnight)).total_seconds())
    if delta < 30 * 60:
        return 0
    return delta // 60


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, half day when the rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


# Response shaping: MongoDB documents -> contract JSON (epoch ms, no _id/id).
def employee_out(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


def record_out(doc: dict) -> dict:
    out = {key: value for key, value in doc.items() if key not in ("_id", "id")}
    for key in ("punch_in", "punch_out"):
        if isinstance(out.get(key), datetime):
            out[key] = dt_to_ms(out[key])
    out.setdefault("half_day", False)  # legacy records may omit it (DATA_MODEL #9)
    for key, default in (("punch_in", None), ("punch_out", None), ("work_hours", None),
                         ("late_minutes", 0), ("overtime_minutes", 0)):
        out.setdefault(key, default)
    history = []
    for entry in out.get("history") or []:
        entry = dict(entry)
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_ms(entry["at"])
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            change = dict(change)
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = dt_to_ms(change[side])
            changes[field] = change
        entry["changes"] = changes
        history.append(entry)
    out["history"] = history
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _ignore_derived_fields(cls, value):
        # The contract explicitly says to ignore client-supplied derived fields.
        if isinstance(value, dict):
            return {key: item for key, item in value.items()
                    if key not in ("work_hours", "late_minutes", "overtime_minutes", "half_day")}
        return value


class EmployeeIn(RequestModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_valid(cls, value: str) -> str:
        iso_date(value)
        return value

    @model_validator(mode="after")
    def _shift_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(RequestModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def _punched_at_valid(cls, value):
        return epoch_ms_field(value)


class PunchOutIn(RequestModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def _punched_at_valid(cls, value):
        return epoch_ms_field(value)


class RegularizeIn(RequestModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", mode="before")
    @classmethod
    def _status_valid(cls, value):
        if value is None:
            raise ValueError("status cannot be null")
        return value

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def _punch_valid(cls, value):
        return epoch_ms_field(value)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """200 only if MongoDB answers a ping, otherwise 503 (contract)."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.fromtimestamp(now_ms() // 1000, tz=UTC)
    try:
        # The unique index on emp_code - not check-then-insert - enforces uniqueness,
        # so concurrent duplicate creates yield exactly one 201, the rest 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)  # R10: total reflects the filter
    items = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)  # contract: sorted by emp_code ascending
        .skip((page - 1) * page_size)  # page 1 is the FIRST page (R10)
        .limit(page_size)
    )
    return {
        "items": [employee_out(doc) for doc in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    # Instant: supplied or now; truncated to whole seconds (R1)
    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else ms_to_dt(now_ms())
    shift_start = emp["shift_start"]
    day = attendance_date(instant, shift_start, emp["shift_end"])  # R1, incl. overnight rule

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": instant,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(instant, shift_start, day),  # R2
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Unique (emp_code, date): simultaneous punch-ins yield exactly one 201.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    # Instant: supplied or now, truncated to whole seconds (R1)
    instant = ms_to_dt(body.punched_at) if body.punched_at is not None else ms_to_dt(now_ms())
    # Most recent record whose punch_in is at or before punched_at (crosses midnight).
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": instant}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in = as_utc(record["punch_in"])
    delta_seconds = int((instant - punch_in).total_seconds())
    if delta_seconds <= 0:
        raise validation_error("punched_at must be after punch_in")
    if delta_seconds > 24 * 3600:
        raise validation_error("punched_at must be within 24 hours after punch_in")

    day = iso_date(record["date"])
    overnight = is_overnight(emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(punch_in, instant)  # R4
    update = {
        "punch_out": instant,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(instant, emp["shift_end"], day, overnight),  # R3
        "half_day": is_half_day(work_hours),  # R5
    }
    # punch_out: None in the filter => among simultaneous punch-outs exactly one wins (200), rest 409.
    updated = db.attendance_logs.find_one_and_update(
        {**record_snapshot(record), "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: CalendarDate, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date.isoformat()})
    if old is None:
        raise HTTPException(404, "no record for this date")

    absence = ("ABSENT", "LEAVE")
    if body.status in absence and (body.punch_in is not None or body.punch_out is not None):
        raise validation_error("ABSENT/LEAVE clears punch times; omit punch_in and punch_out")

    # Final state = stored record + supplied fields (omitted -> unchanged)
    if body.status in absence:
        new_punch_in = new_punch_out = None
    else:
        new_punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        new_punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old.get("punch_out")
    new_status = body.status if body.status is not None else old["status"]

    if new_status in absence and (new_punch_in is not None or new_punch_out is not None):
        raise validation_error("ABSENT/LEAVE records cannot have punch times")
    if new_status not in absence and new_punch_in is None:
        raise validation_error("a presence status requires a punch_in")

    punch_out_dt = None
    if new_punch_in is not None:
        punch_in = ms_to_dt(dt_to_ms(new_punch_in))  # aware, truncated (R1)
        new_punch_in = punch_in
        if attendance_date(punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise validation_error("punch_in must fall on the record's attendance date")
    if new_punch_out is not None:
        punch_out_dt = ms_to_dt(dt_to_ms(new_punch_out))
        new_punch_out = punch_out_dt
        if punch_out_dt <= punch_in:
            raise validation_error("punch_out must be after punch_in")
        if (punch_out_dt - punch_in) > timedelta(hours=24):
            raise validation_error("punch_out must be within 24 hours after punch_in")

    # Recompute derived fields from the final times (R2-R5)
    late = compute_late_minutes(punch_in, emp["shift_start"], date) if new_punch_in is not None else 0
    if new_punch_in is not None and new_punch_out is not None:
        work_hours = compute_work_hours(punch_in, punch_out_dt)
        overnight = is_overnight(emp["shift_start"], emp["shift_end"])
        overtime = compute_overtime_minutes(punch_out_dt, emp["shift_end"], date, overnight)
    else:
        work_hours = None
        overtime = 0
    half_day = is_half_day(work_hours)

    # changes: only fields that actually changed; punch times compared as epoch ms
    def _ms_or_none(value):
        return dt_to_ms(value) if isinstance(value, datetime) else value

    changes = {}
    if old["status"] != new_status:
        changes["status"] = {"from": old["status"], "to": new_status}
    for key, new_value in (("punch_in", new_punch_in), ("punch_out", new_punch_out)):
        if _ms_or_none(old.get(key)) != _ms_or_none(new_value):
            changes[key] = {"from": old.get(key), "to": new_value}  # datetimes -> epoch ms in output
    for key, new_value in (
        ("work_hours", work_hours),
        ("late_minutes", late),
        ("overtime_minutes", overtime),
        ("half_day", half_day),
    ):
        old_value = old.get(key, False if key == "half_day" else (None if key == "work_hours" else 0))
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise validation_error("request changes nothing")

    entry = {
        "at": datetime.fromtimestamp(now_ms() // 1000, tz=UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency: the filter pins the state this patch was computed against;
    # a concurrent PATCH landing first makes it miss -> 409. ($push alone is atomic but
    # could record from/to values computed from a stale base.)
    updated = db.attendance_logs.find_one_and_update(
        record_snapshot(old),
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": work_hours,
                "late_minutes": late,
                "overtime_minutes": overtime,
                "half_day": half_day,
            },
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[CalendarDate] = Query(None),
    date_to: Optional[CalendarDate] = Query(None),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    hint = attendance_hint(emp_code, status)

    # All paging happens inside MongoDB - the collection never lands in Python memory.
    total = db.attendance_logs.count_documents(query, hint=hint)  # R10
    items = (
        db.attendance_logs.find(query, {"_id": 0})
        .hint(hint)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])  # contract sort
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(doc) for doc in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics builders are shared by the real endpoints and executionStats explain.
# --------------------------------------------------------------------------- #
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def attendance_filter(emp_code, date_from, date_to, status):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise validation_error("date_from must be on or before date_to", "query")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    return query


def attendance_hint(emp_code, status):
    if emp_code is not None:
        return "emp_code_1_date_1"
    return "status_1_date_-1_emp_code_1" if status is not None else "date_-1_emp_code_1"


def month_bounds(month: str):
    if not re.fullmatch(MONTH_PATTERN, month):
        raise validation_error("month must be YYYY-MM", "query")
    year, month_number = map(int, month.split("-"))
    try:
        first = date(year, month_number, 1)
        last = date(year, month_number, calendar.monthrange(year, month_number)[1])
    except ValueError:
        raise validation_error("month must be a valid calendar month", "query")
    return first, last


def working_days(first: date, last: date) -> int:
    if first > last:
        return 0
    weeks, remaining = divmod((last - first).days + 1, 7)
    # Only a constant-size remainder is evaluated; no attendance records enter Python.
    return weeks * 5 + sum((first.weekday() + i) % 7 < 5 for i in range(remaining))


def round_half_up_expr(value, places=2):
    """Decimal arithmetic avoids MongoDB $round's half-even and binary-float ties.

    All contract metrics are nonnegative. Preserve null rather than rounding it to zero.
    """
    scale = 10 ** places
    return {"$cond": [{"$eq": [{"$ifNull": [value, None]}, None]}, None,
                      {"$toDouble": {"$divide": [
                          {"$floor": {"$add": [
                              {"$multiply": [{"$toDecimal": value}, scale]},
                              Decimal128("0.5")]}}, scale]}}]}


def weekday_expr(field="$date"):
    return {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": field}}}, [1, 2, 3, 4, 5]]}


def present_expr(weekdays_only=True):
    condition = {"$in": ["$status", PRESENCE]}
    if weekdays_only:
        condition = {"$and": [condition, weekday_expr()]}
    return {"$cond": [condition, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def stats_group():
    """Aggregate stored derived values, including weekend late/overtime/leave totals."""
    valid_hours = {"$and": [{"$in": ["$status", PRESENCE]},
                              {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": present_expr()},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "hours_sum": {"$sum": {"$cond": [valid_hours, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_count": {"$sum": {"$cond": [valid_hours, 1, 0]}},
    }}


def monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            stats_group(), {"$project": {"_id": 0, "hours_sum": 0, "hours_count": 0, "on_duty_count": 0}}]


def summary_pipeline(month, department):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    metrics = ("present_days", "late_count", "total_late_minutes", "hours_sum", "hours_count", "on_duty_count")
    group = {"_id": "$department", "headcount": {"$sum": 1},
             **{key: {"$sum": {"$ifNull": ["$stats." + key, 0]}} for key in metrics},
             "leave_count": {"$sum": {"$ifNull": ["$stats.leave_days", 0]}}}
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                        "$expr": {"$eq": ["$emp_code", "$$code"]}}}, stats_group()], "as": "stats"}},
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}},
        {"$group": group},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
                      "avg_work_hours": round_half_up_expr({"$cond": [
                          {"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None]})}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month, department, limit):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "rank": 1, "total_late_minutes": 1, "late_count": 1,
                      "name": "$employee.name", "department": "$employee.department"}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def validate_trend_range(first, last):
    if last < first or (last - first).days + 1 > 92:
        raise validation_error("trend range must contain 1 to 92 calendar days", "query")


def trend_pipeline(department, first, last):
    validate_trend_range(first, last)
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    # Generate all calendar dates inside MongoDB, even when the department has zero logs.
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$set": {"day": {"$map": {"input": {"$range": [0, (last - first).days + 1]}, "as": "n",
                  "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$n"}}}}}},
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$joined", "as": "joined",
                            "cond": {"$lte": ["$$joined", "$date"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"codes": "$codes", "date": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$date"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None, "present_count": {"$sum": present_expr(False)},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ], "as": "stats"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]},
                  "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": round_half_up_expr({"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, None]}, 4)}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"average": {
            "$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": round_half_up_expr("$average", 4)}},
        {"$sort": {"date": 1}},
    ]


def aggregate(collection, pipeline, hint):
    return list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown employee")
    days = working_days(max(first, iso_date(employee["joined_on"])), last)
    rows = aggregate("attendance_logs", monthly_pipeline(emp_code, month), "emp_code_1_date_1")
    stats = rows[0] if rows else {key: 0 for key in (
        "present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")}
    pct = float((Decimal(str(stats["present_days"])) * 100 / Decimal(days)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)) if days else None
    return {"emp_code": emp_code, "month": month, "working_days": days, **stats, "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = Query(None)):
    hint = "department_1_joined_on_1" if department is not None else "joined_on_1"
    return {"month": month, "items": aggregate("employees", summary_pipeline(month, department), hint)}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = Query(None)):
    return {"month": month, "items": aggregate("attendance_logs", leaderboard_pipeline(month, department, limit),
                                              "date_-1_emp_code_1")}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, first: Annotated[CalendarDate, Query(alias="from")],
                     last: Annotated[CalendarDate, Query(alias="to")]):
    pipeline = trend_pipeline(department, first, last)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": aggregate("employees", pipeline, "department_1_joined_on_1")}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = Query(None), month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None), limit: int = Query(10, ge=1, le=50),
    date_from: Optional[CalendarDate] = Query(None), date_to: Optional[CalendarDate] = Query(None), status: Optional[Status] = Query(None),
    first: Optional[CalendarDate] = Query(None, alias="from"), last: Optional[CalendarDate] = Query(None, alias="to"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        command = {"find": collection, "filter": attendance_filter(emp_code, date_from, date_to, status),
                   "projection": {"_id": 0}, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": attendance_hint(emp_code, status)}
    else:
        if endpoint != "department_trend" and month is None:
            raise validation_error("month is required", "query")
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise validation_error("emp_code is required", "query")
            pipeline, hint = monthly_pipeline(emp_code, month), "emp_code_1_date_1"
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = summary_pipeline(month, department)
            hint = "department_1_joined_on_1" if department is not None else "joined_on_1"
        elif endpoint == "late_leaderboard":
            pipeline, hint = leaderboard_pipeline(month, department, limit), "date_-1_emp_code_1"
        else:
            if department is None or first is None or last is None:
                raise validation_error("department, from and to are required", "query")
            collection = "employees"
            pipeline, hint = trend_pipeline(department, first, last), "department_1_joined_on_1"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}
    result = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
