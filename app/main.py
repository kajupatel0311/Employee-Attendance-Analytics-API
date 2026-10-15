"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding pre-existing OS environment variables
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI") or os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB") or os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# Startup health flag: set False if DB ping or index creation fails at startup.
_startup_ok: bool = True

IST = timezone(timedelta(hours=5, minutes=30))
VALID_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


# Indexes & Lifespan
def init_indexes():
    """Create all required unique and query indexes idempotently at startup."""
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_ok
    # Verify DB connectivity on startup
    try:
        db.command("ping")
    except Exception as e:
        _startup_ok = False
        print(f"ERROR: MongoDB ping failed at startup: {e}")
    # Idempotently create indexes — only attempt when DB is reachable
    if _startup_ok:
        try:
            init_indexes()
        except Exception as e:
            _startup_ok = False
            print(f"ERROR: Failed to create indexes at startup: {e}")
    yield



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# Helpers
def round_half_up(val: float | Decimal, decimals: int) -> float:
    """Half-up rounding (R8) using Python Decimal."""
    d = Decimal(str(val)) if not isinstance(val, Decimal) else val
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """Convert integer epoch ms to UTC datetime truncated to whole seconds (R1)."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def current_utc_truncated() -> datetime:
    """Current UTC datetime truncated to whole seconds (R1)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=0)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Return True if shift_end <= shift_start (R1)."""
    return shift_end <= shift_start


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        punch_time_str = punch_in_ist.strftime("%H:%M")
        if punch_time_str < shift_end:
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, date_str: str, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes.
    A punch-in is late only if strictly more than 10:00 (600s) after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    """
    sh, sm = map(int, shift_start.split(":"))
    y, m, d = map(int, date_str.split("-"))
    start_ist = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = (punch_in_ist - start_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    elapsed_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    hrs = Decimal(elapsed_sec) / Decimal(3600)
    return round_half_up(hrs, 2)


def compute_overtime(punch_out_utc: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    Handles overnight shifts where shift_end falls on next calendar day.
    """
    eh, em = map(int, shift_end.split(":"))
    y, m, d = map(int, date_str.split("-"))
    end_ist = datetime(y, m, d, eh, em, 0, tzinfo=IST)
    if is_overnight_shift(shift_start, shift_end):
        end_ist += timedelta(days=1)
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = (punch_out_ist - end_ist).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Mon-Fri working days in month_str (YYYY-MM).
    For mid-month joiner, counted from joined_on.
    """
    y, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    first_d = date(y, m, 1)
    last_d = date(y, m, num_days)
    try:
        joined_d = date.fromisoformat(joined_on_str)
    except Exception:
        joined_d = first_d
    start_d = max(first_d, joined_d)
    if start_d > last_d:
        return 0
    cnt = 0
    cur = start_d
    while cur <= last_d:
        if cur.weekday() < 5:
            cnt += 1
        cur += timedelta(days=1)
    return cnt


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document into contract AttendanceRecord schema."""
    punch_in_ms = dt_to_epoch_ms(doc.get("punch_in"))
    punch_out_ms = dt_to_epoch_ms(doc.get("punch_out"))
    
    # Format history entries
    raw_history = doc.get("history") or []
    formatted_history = []
    for h in raw_history:
        at_ms = dt_to_epoch_ms(h.get("at"))
        raw_changes = h.get("changes") or {}
        formatted_changes = {}
        for k, change in raw_changes.items():
            from_v = change.get("from")
            to_v = change.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = dt_to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = dt_to_epoch_ms(to_v)
            formatted_changes[k] = {"from": from_v, "to": to_v}
        formatted_history.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": formatted_changes,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document into contract Employee schema."""
    created_at_val = doc.get("created_at")
    if isinstance(created_at_val, datetime):
        created_at_ms = dt_to_epoch_ms(created_at_val)
    elif isinstance(created_at_val, (int, float)):
        created_at_ms = int(created_at_val)
    else:
        created_at_ms = 0
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": created_at_ms,
    }


# Pydantic Schemas
def validate_epoch_millis(v: Any) -> int:
    """Validate strict integer epoch millis between 100000000000 and 4102444800000 (R1)."""
    if v is None:
        return v
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("Must be integer epoch milliseconds")
    if not (100_000_000_000 <= v <= 4_102_444_800_000):
        raise ValueError("Timestamp out of supported millisecond range")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be valid date YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# Canonical shift definitions already supported by attendance & overnight engines
STANDARD_SHIFTS = {
    "GENERAL": {"shift_start": "09:30", "shift_end": "18:30"},
    "MORNING": {"shift_start": "06:00", "shift_end": "15:00"},
    "EVENING": {"shift_start": "14:00", "shift_end": "23:00"},
    "NIGHT": {"shift_start": "21:00", "shift_end": "05:00"},  # Overnight shift
}


class ShiftRotateRequest(BaseModel):
    shift_name: Optional[str] = None
    shift_start: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def validate_shift_rotation(self) -> "ShiftRotateRequest":
        if self.shift_name is not None:
            name_upper = self.shift_name.upper()
            if name_upper not in STANDARD_SHIFTS:
                raise ValueError(f"Unknown shift_name: {self.shift_name}. Must be one of {list(STANDARD_SHIFTS.keys())}")
            # Automatically populate shift_start and shift_end from configuration
            cfg = STANDARD_SHIFTS[name_upper]
            self.shift_start = cfg["shift_start"]
            self.shift_end = cfg["shift_end"]
        else:
            if not self.shift_start or not self.shift_end:
                raise ValueError("Either shift_name or both shift_start and shift_end must be provided")
            if self.shift_start == self.shift_end:
                raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any):
        if v is not None:
            return validate_epoch_millis(v)
        return None

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"Invalid punch-in status: {v}. Must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any):
        if v is not None:
            return validate_epoch_millis(v)
        return None


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_timestamps(cls, v: Any):
        if v is not None:
            return validate_epoch_millis(v)
        return None

    @field_validator("status")
    @classmethod
    def check_status(cls, v: Optional[str]):
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {v}. Must be one of {VALID_STATUSES}")
        return v


# Endpoints
@app.get("/health")
def health():
    """Readiness probe: 200 only if MongoDB is reachable AND startup succeeded, otherwise 503."""
    if not _startup_ok:
        raise HTTPException(status_code=503, detail="Startup failed: database unreachable or indexes not created")
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforce unique emp_code via database index."""
    doc = body.model_dump()
    created_dt = current_utc_truncated()
    doc["created_at"] = created_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    doc.pop("_id", None)
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# Shift notification outbox for offline test assertions and production audit
_notification_outbox: List[Dict[str, Any]] = []


def send_shift_notification(emp_code: str, email: str, name: str, prev_shift: Dict[str, str], new_shift: Dict[str, str]) -> Dict[str, Any]:
    """
    Send shift-rotation notification.
    In testing/dev: stores in outbox and logs without failing or blocking.
    In production: can deliver via SMTP if SMTP_HOST is configured.
    """
    notification = {
        "emp_code": emp_code,
        "recipient": email,
        "recipient_name": name,
        "previous_shift": prev_shift,
        "new_shift": new_shift,
        "timestamp": current_utc_truncated().isoformat(),
        "status": "QUEUED",
    }
    smtp_host = os.environ.get("SMTP_HOST")
    if smtp_host:
        try:
            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = f"Shift Schedule Update - {emp_code}"
            msg["From"] = os.environ.get("SMTP_FROM", "no-reply@hrone.cloud")
            msg["To"] = email
            msg.set_content(
                f"Hello {name},\n\nYour work shift has been updated:\n"
                f"Previous Shift: {prev_shift.get('shift_start')}-{prev_shift.get('shift_end')}\n"
                f"New Shift: {new_shift.get('shift_start')}-{new_shift.get('shift_end')}\n\n"
                f"Regards,\nHR Operations"
            )
            port = int(os.environ.get("SMTP_PORT", 25))
            with smtplib.SMTP(smtp_host, port, timeout=5) as server:
                server.send_message(msg)
            notification["status"] = "SENT"
        except Exception as e:
            notification["status"] = "FAILED"
            notification["error"] = str(e)
    else:
        notification["status"] = "SENT_OFFLINE"

    _notification_outbox.append(notification)
    return notification


@app.patch("/employees/{emp_code}/shift")
def rotate_employee_shift(emp_code: str, body: ShiftRotateRequest):
    """Assign or rotate an employee's shift using existing system configurations."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    old_shift = {
        "shift_start": emp.get("shift_start", "09:30"),
        "shift_end": emp.get("shift_end", "18:30"),
    }

    updated = db.employees.find_one_and_update(
        {"emp_code": emp_code},
        {
            "$set": {
                "shift_start": body.shift_start,
                "shift_end": body.shift_end,
                "updated_at": current_utc_truncated(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    new_shift_info = {
        "shift_name": body.shift_name.upper() if body.shift_name else "CUSTOM",
        "shift_start": updated["shift_start"],
        "shift_end": updated["shift_end"],
    }

    # Dispatch notification only if shift timings actually changed
    notification_status = "UNCHANGED"
    if (old_shift["shift_start"] != body.shift_start) or (old_shift["shift_end"] != body.shift_end):
        notif = send_shift_notification(
            emp_code=emp_code,
            email=updated.get("email", ""),
            name=updated.get("name", ""),
            prev_shift=old_shift,
            new_shift=new_shift_info,
        )
        notification_status = notif.get("status", "SENT_OFFLINE")

    return {
        "emp_code": emp_code,
        "name": updated.get("name"),
        "department": updated.get("department"),
        "previous_shift": old_shift,
        "new_shift": new_shift_info,
        "notification_status": notification_status,
        "message": f"Shift rotated successfully to {updated['shift_start']}-{updated['shift_end']}",
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Atomic insertion guarded by unique (emp_code, date) index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_in_dt = current_utc_truncated()

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    att_date = compute_attendance_date(punch_in_dt, shift_start, shift_end)
    late_min = compute_late_minutes(punch_in_dt, att_date, shift_start)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Atomic conditional update guarantees exactly one 200 under concurrency."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_out_dt = current_utc_truncated()

    # Check if employee has ANY punch-in record
    any_rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not any_rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    # Closes the employee's most recent record whose punch_in is at or before punched_at (spec requirement)
    latest_rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not latest_rec:
        # If any_rec exists but its punch_in > punch_out_dt, this is punched_at <= punch_in
        if punch_out_dt <= any_rec["punch_in"]:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = latest_rec.get("punch_in")
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    # If record already has punch_out, conflict
    if latest_rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    date_str = latest_rec["date"]

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, date_str, shift_start, shift_end)
    half_d = compute_half_day(work_hrs)

    update_fields = {
        "punch_out": punch_out_dt,
        "work_hours": work_hrs,
        "overtime_minutes": ot_min,
        "half_day": half_d,
    }

    # Atomic conditional update on punch_out == None
    res_doc = db.attendance_logs.find_one_and_update(
        {"_id": latest_rec["_id"], "punch_out": None},
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
    )
    if not res_doc:
        raise HTTPException(status_code=409, detail="Already punched out")

    return format_attendance_doc(res_doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Shared query builder for /attendance and /admin/explain/attendance_list."""
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records with efficient database-level pagination and sorting."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status filter")

    q = build_attendance_list_query(emp_code, date_from, date_to, status)
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Manually correct attendance record, recompute derived values, and append audit history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Validate status rules
    current_status = rec.get("status")
    target_status = body.status if body.status is not None else current_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied for ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_utc_dt(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_utc_dt(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    # punch_in must stay on the record's attendance date (R1)
    if final_punch_in is not None:
        calc_date = compute_attendance_date(final_punch_in, shift_start, shift_end)
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    # If punch_out exists, must be after punch_in and within 24h
    if final_punch_in is not None and final_punch_out is not None:
        diff_sec = (final_punch_out - final_punch_in).total_seconds()
        if diff_sec <= 0 or diff_sec > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute derived values
    if final_punch_in is not None:
        final_late_min = compute_late_minutes(final_punch_in, date, shift_start)
    else:
        final_late_min = 0

    if final_punch_in is not None and final_punch_out is not None:
        final_work_hrs = compute_work_hours(final_punch_in, final_punch_out)
        final_ot_min = compute_overtime(final_punch_out, date, shift_start, shift_end)
        final_half_d = compute_half_day(final_work_hrs)
    else:
        final_work_hrs = None
        final_ot_min = 0
        final_half_d = False

    # Compute actual changes
    changes: Dict[str, Dict[str, Any]] = {}
    if target_status != current_status:
        changes["status"] = {"from": current_status, "to": target_status}

    prev_punch_in = rec.get("punch_in")
    if prev_punch_in != final_punch_in:
        changes["punch_in"] = {"from": prev_punch_in, "to": final_punch_in}

    prev_punch_out = rec.get("punch_out")
    if prev_punch_out != final_punch_out:
        changes["punch_out"] = {"from": prev_punch_out, "to": final_punch_out}

    prev_work_hrs = rec.get("work_hours")
    if prev_work_hrs != final_work_hrs:
        changes["work_hours"] = {"from": prev_work_hrs, "to": final_work_hrs}

    prev_late_min = rec.get("late_minutes", 0)
    if prev_late_min != final_late_min:
        changes["late_minutes"] = {"from": prev_late_min, "to": final_late_min}

    prev_ot_min = rec.get("overtime_minutes", 0)
    if prev_ot_min != final_ot_min:
        changes["overtime_minutes"] = {"from": prev_ot_min, "to": final_ot_min}

    prev_half_d = rec.get("half_day", False)
    if prev_half_d != final_half_d:
        changes["half_day"] = {"from": prev_half_d, "to": final_half_d}

    if len(changes) == 0:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    now_utc = current_utc_truncated()
    new_history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency control on history length to prevent losing audit entries
    current_history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$or": [
                {"history": {"$size": current_history_len}},
                {"history": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hrs,
                "late_minutes": final_late_min,
                "overtime_minutes": final_ot_min,
                "half_day": final_half_d,
            },
            "$push": {"history": new_history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


# Analytics Pipelines & Endpoints
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    """Shared pipeline builder for employee monthly summary."""
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = compute_working_days(emp.get("joined_on", "1970-01-01"), month)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        rec = agg_res[0]
        present_days = float(rec.get("present_days", 0.0))
        leave_days = int(rec.get("leave_days", 0))
        late_count = int(rec.get("late_count", 0))
        total_late = int(rec.get("total_late_minutes", 0))
        total_ot = int(rec.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct_raw = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(pct_raw, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/employees/{emp_code}/weekly")
def get_employee_weekly(
    emp_code: str,
    week: Optional[str] = Query(None, description="ISO week in YYYY-Www format, e.g. 2026-W28"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    target_weekly_hours: float = Query(40.0, ge=0.0, le=80.0, description="Target weekly hours (default 40.0)"),
    daily_target_hours: float = Query(8.0, ge=0.0, le=24.0, description="Daily expected working hours (default 8.0)"),
):
    """
    Weekly Working Hours Tracking & Daily Deficit Analysis.
    - Tracks total hours against weekly target (e.g. 40.0h).
    - Identifies specific days with insufficient working hours and flags policy violations.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Determine date range (either from ISO week or explicit date_from/date_to)
    if week:
        match = re.match(r"^(\d{4})-W(0[1-9]|[1-4]\d|5[0-3])$", week)
        if not match:
            raise HTTPException(status_code=422, detail="Invalid week format. Must be YYYY-Www, e.g. 2026-W28")
        yr, wk = int(match.group(1)), int(match.group(2))
        # Monday of ISO week
        start_dt = date.fromisocalendar(yr, wk, 1)
        end_dt = date.fromisocalendar(yr, wk, 7)
    elif date_from and date_to:
        try:
            start_dt = date.fromisoformat(date_from)
            end_dt = date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD")
        if start_dt > end_dt or (end_dt - start_dt).days > 31:
            raise HTTPException(status_code=422, detail="Invalid date range (max 31 days)")
    else:
        raise HTTPException(status_code=422, detail="Either 'week' or both 'date_from' and 'date_to' must be provided")

    start_str = start_dt.isoformat()
    end_str = end_dt.isoformat()

    # Query attendance records in the range
    cursor = db.attendance_logs.find(
        {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}
    )
    logs_by_date = {doc["date"]: doc for doc in cursor}

    days = []
    total_work_hours = 0.0
    working_days_count = 0
    insufficient_days_count = 0
    absent_days_count = 0
    leave_days_count = 0
    unrecorded_days_count = 0

    cur = start_dt
    while cur <= end_dt:
        cur_str = cur.isoformat()
        is_weekend = cur.weekday() >= 5
        log = logs_by_date.get(cur_str)

        if log:
            status = log.get("status", "PRESENT")
            work_hours = float(log.get("work_hours") or 0.0)
            half_day = bool(log.get("half_day", False))
            late_minutes = int(log.get("late_minutes", 0))
            is_recorded = True
        else:
            status = None if not is_weekend else "WEEKEND"
            work_hours = 0.0
            half_day = False
            late_minutes = 0
            is_recorded = False

        total_work_hours += work_hours
        if not is_weekend:
            working_days_count += 1

        # Determine daily compliance flag strictly following OpenAPI & DATA_MODEL rules
        if is_weekend:
            flag = "WEEKEND"
            comment = "Weekend - non-working day"
        elif not is_recorded:
            flag = "UNRECORDED"
            comment = "No attendance record found for scheduled working day"
            unrecorded_days_count += 1
            insufficient_days_count += 1
        elif status == "LEAVE":
            flag = "LEAVE"
            comment = "Approved leave on scheduled working day"
            leave_days_count += 1
            insufficient_days_count += 1
        elif status == "ABSENT":
            flag = "ABSENT"
            comment = "Recorded as absent on scheduled working day"
            absent_days_count += 1
            insufficient_days_count += 1
        elif half_day or (work_hours < 4.50):
            flag = "HALF_DAY_DEFICIT"
            comment = f"Worked {work_hours}h (below half-day 4.50h threshold)"
            insufficient_days_count += 1
        elif work_hours < daily_target_hours:
            shortfall = round_half_up(daily_target_hours - work_hours, 2)
            flag = "INSUFFICIENT_HOURS"
            comment = f"Worked {work_hours}h vs {daily_target_hours}h target ({shortfall}h deficit)"
            insufficient_days_count += 1
        else:
            flag = "COMPLIANT"
            comment = f"Completed required hours ({work_hours}h)"

        days.append({
            "date": cur_str,
            "day_of_week": cur.strftime("%A"),
            "is_weekend": is_weekend,
            "status": status,
            "work_hours": round_half_up(work_hours, 2),
            "expected_hours": 0.0 if is_weekend else daily_target_hours,
            "flag": flag,
            "comment": comment,
            "late_minutes": late_minutes,
        })
        cur += timedelta(days=1)

    rounded_total = round_half_up(total_work_hours, 2)
    shortfall = round_half_up(max(0.0, target_weekly_hours - rounded_total), 2)
    is_compliant = rounded_total >= target_weekly_hours

    return {
        "emp_code": emp_code,
        "employee_name": emp.get("name"),
        "department": emp.get("department"),
        "period": {
            "week": week,
            "date_from": start_str,
            "date_to": end_str,
        },
        "target_weekly_hours": target_weekly_hours,
        "actual_weekly_hours": rounded_total,
        "shortfall_hours": shortfall,
        "is_compliant": is_compliant,
        "summary": {
            "working_days": working_days_count,
            "insufficient_days": insufficient_days_count,
            "absent_days": absent_days_count,
            "leave_days": leave_days_count,
            "unrecorded_days": unrecorded_days_count,
        },
        "days": days,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    """Shared pipeline builder for department summary."""
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_list": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emp_list"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "date": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "work_hours": 1,
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "total_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "stat": {"$arrayElemAt": ["$stats", 0]},
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {"$ifNull": ["$stat.present_days", 0.0]},
                "total_work_hours": {"$ifNull": ["$stat.total_work_hours", 0.0]},
                "work_hours_count": {"$ifNull": ["$stat.work_hours_count", 0]},
                "late_count": {"$ifNull": ["$stat.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$stat.total_late_minutes", 0]},
                "leave_count": {"$ifNull": ["$stat.leave_count", 0]},
                "on_duty_count": {"$ifNull": ["$stat.on_duty_count", 0]},
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary aggregation across employees and logs."""
    pipeline = build_department_summary_pipeline(month, department)
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        wh_count = d.get("work_hours_count", 0)
        tot_wh = d.get("total_work_hours", 0.0)
        if wh_count > 0:
            avg_wh = round_half_up(tot_wh / wh_count, 2)
        else:
            avg_wh = None

        items.append({
            "department": d.get("department"),
            "headcount": d.get("headcount", 0),
            "present_days": float(d.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": d.get("late_count", 0),
            "total_late_minutes": d.get("total_late_minutes", 0),
            "leave_count": d.get("leave_count", 0),
            "on_duty_count": d.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Shared pipeline builder for late leaderboard."""
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipe = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipe


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard using standard competition ranking ($rank)."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> List[Dict[str, Any]]:
    """Shared pipeline builder for daily department attendance trend."""
    from_dt = datetime.fromisoformat(from_date + "T00:00:00Z")
    to_dt = datetime.fromisoformat(to_date + "T00:00:00Z") + timedelta(days=1)

    return [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dow": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "cnt"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.cnt", 0]}, 0]},
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                {"$gt": [{"$ifNull": [{"$arrayElemAt": ["$hc.cnt", 0]}, 0]}, 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {"$ifNull": ["$present_count", 0.0]},
                                {"$arrayElemAt": ["$hc.cnt", 0]},
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Daily attendance trend with database gap-filling and moving average."""
    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        f_d = date.fromisoformat(from_date)
        t_d = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if t_d < f_d:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (t_d - f_d).days > 92:
        raise HTTPException(status_code=422, detail="Range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for row in raw_items:
        rate = row.get("attendance_rate")
        mov = row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(mov, 4) if mov is not None else None,
        })

    return {"department": department, "items": items}


@app.get("/analytics/daily-summary")
def get_daily_summary(
    target_date: Optional[str] = Query(None, alias="date", description="Target calendar date YYYY-MM-DD (defaults to today in IST)"),
    department: Optional[str] = Query(None, description="Optional department filter"),
):
    """
    Real-time daily attendance coordination summary for HR and management.
    Summarizes today's present, late, on-duty, WFH, on-leave, and unrecorded headcount.
    """
    if target_date is None:
        target_date = datetime.now(IST).strftime("%Y-%m-%d")
    else:
        try:
            date.fromisoformat(target_date)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD")

    # Match employees active on or before target date
    emp_filter: Dict[str, Any] = {"joined_on": {"$lte": target_date}}
    if department:
        emp_filter["department"] = department

    active_emps = list(db.employees.find(emp_filter, {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "shift_start": 1, "shift_end": 1}))
    total_headcount = len(active_emps)

    if total_headcount == 0:
        return {
            "date": target_date,
            "department": department or "ALL",
            "headcount": 0,
            "present_count": 0,
            "wfh_count": 0,
            "on_duty_count": 0,
            "late_count": 0,
            "leave_count": 0,
            "absent_count": 0,
            "unrecorded_count": 0,
            "attendance_rate": None,
            "roster": [],
        }

    emp_codes = [e["emp_code"] for e in active_emps]
    emp_map = {e["emp_code"]: e for e in active_emps}

    logs_cursor = db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": target_date})
    logs_map = {l["emp_code"]: l for l in logs_cursor}

    present_c = 0
    wfh_c = 0
    on_duty_c = 0
    late_c = 0
    leave_c = 0
    absent_c = 0
    unrecorded_c = 0

    roster = []
    for code, emp in emp_map.items():
        log = logs_map.get(code)
        if log:
            st = log.get("status", "PRESENT")
            late_m = int(log.get("late_minutes", 0))
            wh = log.get("work_hours")
            is_late = late_m > 0

            if st == "PRESENT":
                present_c += 1
            elif st == "WFH":
                wfh_c += 1
            elif st == "ON_DUTY":
                on_duty_c += 1
            elif st == "LEAVE":
                leave_c += 1
            elif st == "ABSENT":
                absent_c += 1

            if is_late:
                late_c += 1

            roster.append({
                "emp_code": code,
                "name": emp.get("name"),
                "department": emp.get("department"),
                "shift": f"{emp.get('shift_start')}-{emp.get('shift_end')}",
                "status": st,
                "punch_in": dt_to_epoch_ms(log.get("punch_in")),
                "punch_out": dt_to_epoch_ms(log.get("punch_out")),
                "late_minutes": late_m,
                "work_hours": wh,
            })
        else:
            unrecorded_c += 1
            roster.append({
                "emp_code": code,
                "name": emp.get("name"),
                "department": emp.get("department"),
                "shift": f"{emp.get('shift_start')}-{emp.get('shift_end')}",
                "status": "UNRECORDED",
                "punch_in": None,
                "punch_out": None,
                "late_minutes": 0,
                "work_hours": None,
            })

    total_attending = present_c + wfh_c + on_duty_c
    att_rate = round_half_up(total_attending / total_headcount, 4) if total_headcount > 0 else 0.0

    return {
        "date": target_date,
        "department": department or "ALL",
        "headcount": total_headcount,
        "present_count": present_c,
        "wfh_count": wfh_c,
        "on_duty_count": on_duty_c,
        "late_count": late_c,
        "leave_count": leave_c,
        "absent_count": absent_c,
        "unrecorded_count": unrecorded_c,
        "attendance_rate": att_rate,
        "roster": roster,
    }


# Explain Endpoint
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    request: Request,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Run MongoDB explain with executionStats verbosity for an endpoint's main query."""
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint. Must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        plan = db.command("explain", find_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipe = build_employee_monthly_pipeline(emp_code, month)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipe = build_department_summary_pipeline(month, department)
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipe,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipe = build_late_leaderboard_pipeline(month, limit, department)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        # Check alias 'from' / 'to' from request if query params were passed as from / to
        q_params = request.query_params
        eff_from = from_date or q_params.get("from")
        eff_to = to_date or q_params.get("to")
        eff_dept = department or q_params.get("department")

        if not eff_dept or not eff_from or not eff_to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            f_d = date.fromisoformat(eff_from)
            t_d = date.fromisoformat(eff_to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if t_d < f_d or (t_d - f_d).days > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        pipe = build_department_trend_pipeline(eff_dept, eff_from, eff_to)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
