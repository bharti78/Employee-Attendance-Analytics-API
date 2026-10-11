"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: Create indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    """Create all required indexes idempotently."""
    # Employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    
    # Attendance logs
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, UTC)


def datetime_to_epoch_ms(dt: datetime) -> int:
    """Convert UTC datetime to epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def to_ist_date(dt: datetime) -> str:
    """Convert UTC datetime to IST date string (YYYY-MM-DD)."""
    ist_dt = dt.astimezone(IST)
    return ist_dt.date().isoformat()


def parse_time_to_ist_datetime(time_str: str, date_str: str) -> datetime:
    """Parse HH:MM time string and YYYY-MM-DD date to IST datetime, then convert to UTC."""
    h, m = map(int, time_str.split(":"))
    ist_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    return ist_dt.astimezone(UTC)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """Compute late minutes per R2. Handles overnight shifts (R1)."""
    shift_start_utc = parse_time_to_ist_datetime(shift_start, date_str)
    
    # Truncate to seconds (R1)
    punch_in_utc = truncate_to_seconds(punch_in_utc)
    
    # For overnight shifts, check if punch-in is before shift_end (belongs to previous day's shift)
    emp_shift_start = shift_start_utc
    
    seconds_diff = (punch_in_utc - emp_shift_start).total_seconds()
    minutes = int(seconds_diff / 60)
    
    # Grace period: only late if strictly more than 10 minutes after shift_start (R2)
    if minutes > 10:
        return minutes
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Compute work hours per R4 with half-up rounding."""
    punch_in_utc = truncate_to_seconds(punch_in_utc)
    punch_out_utc = truncate_to_seconds(punch_out_utc)
    
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(seconds / 3600))
    # Round to 2 decimals half-up (R4)
    rounded = hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, date_str: str, 
                             shift_start: str) -> int:
    """Compute overtime minutes per R3. Handles overnight shifts."""
    # Determine the correct date for shift_end based on overnight shift logic
    h, m = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    
    # If shift_end <= shift_start, it's an overnight shift
    if h < sh or (h == sh and m <= sm):
        # Shift ends on the next calendar day
        from datetime import timedelta
        next_day = datetime.fromisoformat(date_str) + timedelta(days=1)
        shift_end_date = next_day.isoformat()
    else:
        shift_end_date = date_str
    
    shift_end_utc = parse_time_to_ist_datetime(shift_end, shift_end_date)
    punch_out_utc = truncate_to_seconds(punch_out_utc)
    
    seconds_diff = (punch_out_utc - shift_end_utc).total_seconds()
    minutes = int(seconds_diff / 60)
    
    # Only count if >= 30 minutes (R3)
    if minutes >= 30:
        return minutes
    return 0


def compute_half_day(work_hours: float) -> bool:
    """Determine if half day per R5."""
    return work_hours < 4.50


def is_working_day(date_str: str) -> bool:
    """Check if date is Monday-Friday (R7)."""
    dt = datetime.fromisoformat(date_str)
    return dt.weekday() < 5  # 0=Monday, 6=Sunday


def round_half_up(value: float, decimals: int) -> float:
    """Round value to specified decimals using half-up rounding (R8)."""
    decimal_value = Decimal(str(value))
    quant_str = '0.' + '0' * (decimals - 1) + '1' if decimals > 0 else '1'
    rounded = decimal_value.quantize(Decimal(quant_str), rounding=ROUND_HALF_UP)
    return float(rounded)


def convert_doc_for_response(doc: dict) -> dict:
    """Convert MongoDB document to API response format."""
    doc = doc.copy()
    doc.pop("_id", None)
    
    # Convert datetime fields to epoch milliseconds
    if "created_at" in doc and doc["created_at"] is not None:
        if isinstance(doc["created_at"], datetime):
            doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    
    if "punch_in" in doc and doc["punch_in"] is not None:
        if isinstance(doc["punch_in"], datetime):
            doc["punch_in"] = datetime_to_epoch_ms(doc["punch_in"])
    
    if "punch_out" in doc and doc["punch_out"] is not None:
        if isinstance(doc["punch_out"], datetime):
            doc["punch_out"] = datetime_to_epoch_ms(doc["punch_out"])
    
    # Convert history datetime fields
    if "history" in doc and doc["history"]:
        for entry in doc["history"]:
            if "at" in entry and entry["at"] is not None:
                if isinstance(entry["at"], datetime):
                    entry["at"] = datetime_to_epoch_ms(entry["at"])
            if "changes" in entry:
                for field, change in entry["changes"].items():
                    if "from" in change and change["from"] is not None:
                        if isinstance(change["from"], datetime):
                            change["from"] = datetime_to_epoch_ms(change["from"])
                    if "to" in change and change["to"] is not None:
                        if isinstance(change["to"], datetime):
                            change["to"] = datetime_to_epoch_ms(change["to"])
    
    # Ensure half_day exists (legacy records may not have it)
    if "half_day" not in doc:
        doc["half_day"] = False
    
    # Ensure history exists
    if "history" not in doc:
        doc["history"] = []
    
    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    
    @field_validator('shift_start', 'shift_end')
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        import re
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError('Time must be HH:MM format')
        return v
    
    @field_validator('emp_code')
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        import re
        if not re.match(r'^EMP\d{4,6}$', v):
            raise ValueError('emp_code must be EMP followed by 4-6 digits')
        return v
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError('Invalid email format')
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"
    
    @field_validator('punched_at')
    @classmethod
    def validate_epoch_ms(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100000000000 or v > 4102444800000:
                raise ValueError('punched_at must be valid epoch milliseconds')
        return v
    
    @field_validator('status')
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ["PRESENT", "WFH", "ON_DUTY"]:
            raise ValueError('status must be PRESENT, WFH, or ON_DUTY')
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    
    @field_validator('punched_at')
    @classmethod
    def validate_epoch_ms(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100000000000 or v > 4102444800000:
                raise ValueError('punched_at must be valid epoch milliseconds')
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str
    
    @field_validator('status')
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise ValueError('Invalid status value')
        return v
    
    @field_validator('punch_in', 'punch_out')
    @classmethod
    def validate_epoch_ms(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100000000000 or v > 4102444800000:
                raise ValueError('Must be valid epoch milliseconds')
        return v


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Health check - returns 200 only if MongoDB answers."""
    try:
        db.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB not available")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code."""
    # Validate shift_start != shift_end
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")
    
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    
    try:
        db.employees.insert_one(doc)
    except Exception as e:
        if "duplicate key" in str(e).lower():
            raise HTTPException(status_code=409, detail="emp_code already exists")
        raise
    
    return convert_doc_for_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    """List employees, filtered by department, paginated, sorted by emp_code."""
    q = {}
    if department:
        q["department"] = department
    
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    items = [convert_doc_for_response(doc) for doc in items]
    
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Race-safe using unique index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Convert punched_at to UTC datetime
    if body.punched_at:
        punch_in_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_utc = datetime.now(UTC)
    
    # Determine attendance date per R1
    date_str = to_ist_date(punch_in_utc)
    
    # For overnight shifts, check if punch-in belongs to previous day's shift
    h, m = map(int, emp["shift_end"].split(":"))
    sh, sm = map(int, emp["shift_start"].split(":"))
    if h < sh or (h == sh and m <= sm):
        # Overnight shift: check if punch time is before shift_end
        shift_end_ist = parse_time_to_ist_datetime(emp["shift_end"], date_str)
        punch_in_ist = punch_in_utc.astimezone(IST)
        if punch_in_ist < shift_end_ist:
            # Belongs to previous day's shift
            from datetime import timedelta
            prev_day = (datetime.fromisoformat(date_str) - timedelta(days=1)).isoformat()
            date_str = prev_day
    
    # Try to insert with unique index handling race condition
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in_utc, emp["shift_start"], date_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except Exception as e:
        if "duplicate key" in str(e).lower():
            raise HTTPException(status_code=409, detail="already punched in for this date")
        raise
    
    return convert_doc_for_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Finds most recent punch-in and closes it."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    if body.punched_at:
        punch_out_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_utc = datetime.now(UTC)
    
    # Find most recent record with punch_in at or before punched_at, no punch_out yet
    # This must work across midnight for overnight shifts
    query = {
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punch_out_utc},
        "punch_out": None
    }
    
    record = db.attendance_logs.find_one(query, sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(status_code=404, detail="No open punch-in found")
    
    punch_in_utc = record["punch_in"]
    
    # Validate punch_out > punch_in
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    
    # Validate within 24 hours
    if (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
    
    # Compute derived fields
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime_minutes(punch_out_utc, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)
    
    # Update atomically with check for concurrent punch-outs
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="Record already punched out")
    
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return convert_doc_for_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    """List attendance records with filters and pagination."""
    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    
    q = {}
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
    
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    
    # Sort by date descending, then emp_code ascending
    items = list(db.attendance_logs.find(q)
                 .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                 .skip(skip)
                 .limit(page_size))
    items = [convert_doc_for_response(doc) for doc in items]
    
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Manually correct an attendance record with audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    
    # Build update and track changes
    updates = {}
    changes = {}
    
    # Handle status change
    if body.status is not None and body.status != record.get("status"):
        # If setting to ABSENT or LEAVE, must not have punch times
        if body.status in ["ABSENT", "LEAVE"]:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="Cannot set punch times with ABSENT/LEAVE status")
            updates["status"] = body.status
            updates["punch_in"] = None
            updates["punch_out"] = None
            updates["work_hours"] = None
            changes["status"] = {"from": record.get("status"), "to": body.status}
            if record.get("punch_in") is not None:
                changes["punch_in"] = {"from": datetime_to_epoch_ms(record["punch_in"]), "to": None}
            if record.get("punch_out") is not None:
                changes["punch_out"] = {"from": datetime_to_epoch_ms(record["punch_out"]), "to": None}
            if record.get("work_hours") is not None:
                changes["work_hours"] = {"from": record.get("work_hours"), "to": None}
        else:
            # Presence status requires punch_in
            if body.punch_in is None and record.get("punch_in") is None:
                raise HTTPException(status_code=422, detail="Presence status requires punch_in")
            updates["status"] = body.status
            changes["status"] = {"from": record.get("status"), "to": body.status}
    
    # Handle punch_in change
    if body.punch_in is not None:
        new_punch_in = epoch_ms_to_datetime(body.punch_in)
        # Validate punch_in is on the record's attendance date (R1)
        if to_ist_date(new_punch_in) != date:
            raise HTTPException(status_code=422, detail="punch_in must be on the attendance date")
        
        if record.get("punch_in") is None or new_punch_in != record["punch_in"]:
            updates["punch_in"] = new_punch_in
            if record.get("punch_in") is None:
                changes["punch_in"] = {"from": None, "to": body.punch_in}
            else:
                changes["punch_in"] = {"from": datetime_to_epoch_ms(record["punch_in"]), "to": body.punch_in}
    
    # Handle punch_out change
    if body.punch_out is not None:
        new_punch_out = epoch_ms_to_datetime(body.punch_out)
        punch_in = updates.get("punch_in", record.get("punch_in"))
        if punch_in is None:
            raise HTTPException(status_code=422, detail="Cannot set punch_out without punch_in")
        if new_punch_out <= punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out - punch_in).total_seconds() > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        
        if record.get("punch_out") is None or new_punch_out != record["punch_out"]:
            updates["punch_out"] = new_punch_out
            if record.get("punch_out") is None:
                changes["punch_out"] = {"from": None, "to": body.punch_out}
            else:
                changes["punch_out"] = {"from": datetime_to_epoch_ms(record["punch_out"]), "to": body.punch_out}
    
    # If no changes, return 422
    if not changes:
        raise HTTPException(status_code=422, detail="No changes to apply")
    
    # Recpute derived fields based on final times
    final_punch_in = updates.get("punch_in", record.get("punch_in"))
    final_punch_out = updates.get("punch_out", record.get("punch_out"))
    final_status = updates.get("status", record.get("status"))
    
    if final_status in ["ABSENT", "LEAVE"]:
        updates["work_hours"] = None
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
        if record.get("late_minutes") != 0:
            changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": 0}
        if record.get("overtime_minutes") != 0:
            changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": 0}
        if record.get("half_day") != False:
            changes["half_day"] = {"from": record.get("half_day", False), "to": False}
    elif final_punch_in is not None:
        # Recompute late_minutes
        new_late = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if new_late != record.get("late_minutes", 0):
            updates["late_minutes"] = new_late
            changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
        
        if final_punch_out is not None:
            # Recompute work_hours, overtime, half_day
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime = compute_overtime_minutes(final_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
            
            if new_work_hours != record.get("work_hours"):
                updates["work_hours"] = new_work_hours
                changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
            if new_overtime != record.get("overtime_minutes", 0):
                updates["overtime_minutes"] = new_overtime
                changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime}
            if new_half_day != record.get("half_day", False):
                updates["half_day"] = new_half_day
                changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}
    
    # Append history entry
    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    updates["history"] = record.get("history", []) + [history_entry]
    
    # Update atomically
    result = db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": updates}
    )
    
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return convert_doc_for_response(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Calculate working days (Mon-Fri) from joined_on (R7)
    year, month_num = map(int, month.split("-"))
    from calendar import monthrange
    days_in_month = monthrange(year, month_num)[1]
    
    joined_date = datetime.fromisoformat(emp["joined_on"])
    working_days = 0
    for day in range(1, days_in_month + 1):
        date_str = f"{year}-{month_num:02d}-{day:02d}"
        if is_working_day(date_str):
            if datetime.fromisoformat(date_str) >= joined_date:
                working_days += 1
    
    # Aggregation pipeline
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month}"}
        }},
        {"$addFields": {
            "is_present": {
                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
            },
            "is_working_day": {
                "$cond": {
                    "if": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                    "then": True,
                    "else": False
                }
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": {
                        "if": {"$and": ["$is_present", "$is_working_day"]},
                        "then": {"$cond": {"if": "$half_day", "then": 0.5, "else": 1.0}},
                        "else": 0
                    }
                }
            },
            "leave_days": {
                "$sum": {"$cond": {"if": {"$eq": ["$status", "LEAVE"]}, "then": 1, "else": 0}}
            },
            "late_count": {
                "$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]
    
    result = list(db.attendance_logs.aggregate(pipeline))
    data = result[0] if result else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0
    }
    
    # Calculate attendance percentage
    if working_days > 0:
        attendance_pct = round_half_up((data["present_days"] / working_days) * 100, 2)
    else:
        attendance_pct = None
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": data["present_days"],
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    """Department summary for a month, cross-collection aggregation."""
    # Build match for department filter
    dept_match = {"department": department} if department else {}
    
    # First, get headcount per department (R9: employees with joined_on <= month end)
    year, month_num = map(int, month.split("-"))
    from calendar import monthrange
    last_day = monthrange(year, month_num)[1]
    month_end = f"{year}-{month_num:02d}-{last_day:02d}"
    
    headcount_pipeline = [
        {"$match": {
            "joined_on": {"$lte": month_end},
            **dept_match
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1}
        }}
    ]
    
    headcounts = {doc["_id"]: doc["headcount"] for doc in db.employees.aggregate(headcount_pipeline)}
    
    # Now aggregate attendance logs
    match_stage = {
        "date": {"$regex": f"^{month}"}
    }
    if department:
        # Need to join with employees to filter by department
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        match_stage["emp_code"] = {"$in": emp_codes}
    
    attendance_pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"},
        {"$addFields": {
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {
                "$sum": {
                    "$cond": {
                        "if": {"$and": [
                            "$is_present",
                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                        ]},
                        "then": {"$cond": {"if": "$half_day", "then": 0.5, "else": 1.0}},
                        "else": 0
                    }
                }
            },
            "total_work_hours": {
                "$sum": {
                    "$cond": {
                        "if": {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                        "then": "$work_hours",
                        "else": 0
                    }
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": {
                        "if": {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                        "then": 1,
                        "else": 0
                    }
                }
            },
            "late_count": {
                "$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {
                "$sum": {"$cond": {"if": {"$eq": ["$status", "LEAVE"]}, "then": 1, "else": 0}}
            },
            "on_duty_count": {
                "$sum": {"$cond": {"if": {"$eq": ["$status", "ON_DUTY"]}, "then": 1, "else": 0}}
            }
        }}
    ]
    
    attendance_data = list(db.attendance_logs.aggregate(attendance_pipeline))
    
    # Merge headcount and attendance data
    items = []
    for dept_data in attendance_data:
        dept = dept_data["_id"]
        if dept not in headcounts or headcounts[dept] == 0:
            continue
        
        avg_work_hours = None
        if dept_data["work_hours_count"] > 0:
            avg_work_hours = round_half_up(dept_data["total_work_hours"] / dept_data["work_hours_count"], 2)
        
        items.append({
            "department": dept,
            "headcount": headcounts[dept],
            "present_days": dept_data["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": dept_data["late_count"],
            "total_late_minutes": dept_data["total_late_minutes"],
            "leave_count": dept_data["leave_count"],
            "on_duty_count": dept_data["on_duty_count"]
        })
    
    # Sort by department ascending
    items.sort(key=lambda x: x["department"])
    
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None
):
    """Late-comers leaderboard with standard competition ranking."""
    # Build match stage
    match_stage = {
        "date": {"$regex": f"^{month}"},
        "late_minutes": {"$gt": 0}
    }
    if department:
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        match_stage["emp_code"] = {"$in": emp_codes}
    
    # Aggregation with window function for ranking
    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING, "_id": ASCENDING},
            "output": {
                "rank": {
                    "$denseRank": {}
                }
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ]
    
    items = list(db.attendance_logs.aggregate(pipeline))
    
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    """Daily attendance trend with gap filling and 7-day moving average."""
    # Validate department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    
    # Validate date range
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    
    # Calculate range length
    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date)
    if (to_dt - from_dt).days > 92:
        raise HTTPException(status_code=422, detail="Range must not exceed 92 days")
    
    # Get all employees in department
    emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1})]
    
    # Build aggregation with gap filling
    pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$addFields": {
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": {
                        "if": "$is_present",
                        "then": {"$cond": {"if": "$half_day", "then": 0.5, "else": 1.0}},
                        "else": 0
                    }
                }
            },
            "late_count": {
                "$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}
            }
        }},
        {"$sort": {"_id": ASCENDING}},
        {"$densify": {
            "field": "_id",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": ["full", "full"]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"_id": ASCENDING},
            "output": {
                "attendance_rate": {
                    "$movingAvg": {
                        "input": "$present_count",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        }}
    ]
    
    # Note: $densify and $movingAvg require MongoDB 5.0+
    # For compatibility, we'll do gap filling in Python
    
    # Get aggregated data
    agg_pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$addFields": {
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": {
                        "if": "$is_present",
                        "then": {"$cond": {"if": "$half_day", "then": 0.5, "else": 1.0}},
                        "else": 0
                    }
                }
            },
            "late_count": {
                "$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}
            }
        }},
        {"$sort": {"_id": ASCENDING}}
    ]
    
    daily_data = {doc["_id"]: doc for doc in db.attendance_logs.aggregate(agg_pipeline)}
    
    # Fill gaps and compute moving average
    items = []
    current_date = from_dt
    rates = []  # Store non-null rates for moving average
    
    while current_date <= to_dt:
        date_str = current_date.date().isoformat()
        is_working = is_working_day(date_str)
        
        # Calculate headcount for this day
        headcount = sum(1 for e in db.employees.find({"department": department, "joined_on": {"$lte": date_str}}))
        
        data = daily_data.get(date_str, {"present_count": 0, "late_count": 0})
        present_count = data["present_count"]
        late_count = data["late_count"]
        
        # Calculate attendance rate
        attendance_rate = None
        if is_working and headcount > 0:
            attendance_rate = round_half_up((present_count / headcount) * 100, 4)
        
        # Add to rates list for moving average
        rates.append(attendance_rate)
        
        # Calculate 7-day moving average (non-null rates only)
        moving_avg = None
        window_rates = [r for r in rates if r is not None]
        if window_rates:
            moving_avg = round_half_up(sum(window_rates) / len(window_rates), 4)
        
        items.append({
            "date": date_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg
        })
        
        current_date += timedelta(days=1)
        # Keep only last 7 rates
        if len(rates) > 7:
            rates.pop(0)
    
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = None,
    page_size: Optional[int] = None
):
    """Return MongoDB explain output for an endpoint's main query."""
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint. Must be one of: {', '.join(valid_endpoints)}")
    
    collection = None
    pipeline_or_query = None
    
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
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
        
        skip = (page - 1) * page_size if page and page_size else 0
        limit_val = page_size if page_size else 20
        
        pipeline_or_query = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": limit_val
        }
        explain_result = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(limit_val).explain()
    
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        collection = "attendance_logs"
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}"}
            }},
            {"$addFields": {
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
            }},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": {"if": "$is_present", "then": 1, "else": 0}}},
                "leave_days": {"$sum": {"$cond": {"if": {"$eq": ["$status", "LEAVE"]}, "then": 1, "else": 0}}},
                "late_count": {"$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }}
        ]
        explain_result = db.attendance_logs.aggregate(pipeline).explain()
    
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        match_stage = {"date": {"$regex": f"^{month}"}}
        if department:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
            match_stage["emp_code"] = {"$in": emp_codes}
        
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }},
            {"$unwind": "$employee"},
            {"$group": {
                "_id": "$employee.department",
                "present_days": {"$sum": 1},
                "late_count": {"$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}},
                "total_late_minutes": {"$sum": "$late_minutes"}
            }}
        ]
        explain_result = db.attendance_logs.aggregate(pipeline).explain()
    
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        match_stage = {
            "date": {"$regex": f"^{month}"},
            "late_minutes": {"$gt": 0}
        }
        if department:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
            match_stage["emp_code"] = {"$in": emp_codes}
        
        limit_val = limit if limit else 10
        
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }},
            {"$unwind": "$employee"},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {"$limit": limit_val}
        ]
        explain_result = db.attendance_logs.aggregate(pipeline).explain()
    
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        collection = "attendance_logs"
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        
        pipeline = [
            {"$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": 1},
                "late_count": {"$sum": {"$cond": {"if": {"$gt": ["$late_minutes", 0]}, "then": 1, "else": 0}}}
            }},
            {"$sort": {"_id": ASCENDING}}
        ]
        explain_result = db.attendance_logs.aggregate(pipeline).explain()
    
    # Convert explain result to dict (remove CommandCursor if present)
    if hasattr(explain_result, 'command_cursor'):
        explain_result = dict(explain_result)
    
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_result
    }
