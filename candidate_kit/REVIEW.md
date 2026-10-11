# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | health (line 72-74) | Returns 200 always without checking MongoDB | Health check would pass even if MongoDB is down | Added db.command('ping') with try/except, return 503 on failure |
| 2 | create_employee (line 82) | created_at is naive datetime (no timezone) | API contract requires epoch milliseconds from UTC | Changed to datetime.now(UTC) and convert to epoch ms in response |
| 3 | create_employee (line 83) | No unique index on emp_code | Concurrent duplicate creates could both succeed | Added unique index on emp_code at startup, use try/except for duplicate key error |
| 4 | create_employee | No validation that shift_start != shift_end | Could create invalid shift configuration | Added validation to raise 422 if equal |
| 5 | list_employees (line 93) | skip = page * page_size is wrong formula | Page 1 would skip 20 items instead of 0 | Changed to (page - 1) * page_size |
| 6 | list_employees (line 94) | count_documents({}) counts all employees | total would ignore department filter | Changed to count_documents(q) with the actual query |
| 7 | list_employees (line 95) | No sorting by emp_code ascending | Results not sorted as required by OpenAPI | Added .sort("emp_code", ASCENDING) |
| 8 | list_employees | Returns created_at as datetime | API contract requires epoch milliseconds | Added convert_doc_for_response() to convert all datetime fields |
| 9 | punch_in (line 102) | datetime.fromtimestamp creates naive datetime | Timezone handling incorrect, violates R1 | Changed to epoch_ms_to_datetime() with UTC timezone |
| 10 | punch_in (line 103) | ts.date().isoformat() uses local date | Attendance date should be IST per R1 | Changed to to_ist_date() converting UTC to IST date |
| 11 | punch_in (line 104) | Check-then-insert is not race-safe | Two concurrent punch-ins could both pass check | Relied on unique index (emp_code, date) to handle race condition |
| 12 | punch_in (line 110) | Stores punch_in as naive datetime | Should be UTC datetime in MongoDB | Changed to store UTC datetime |
| 13 | punch_in (line 119) | Returns "id" field which shouldn't exist | API doesn't use MongoDB _id | Removed id field, only return fields from schema |
| 14 | punch_in | Returns punch_in as datetime | API contract requires epoch milliseconds | Added convert_doc_for_response() to convert |
| 15 | punch_in | No validation that status is PRESENT/WFH/ON_DUTY | Could accept invalid status values | Added Pydantic validator to restrict to valid values |
| 16 | punch_in | No validation of punched_at as epoch milliseconds | Could accept seconds, floats, strings | Added Pydantic validator to check range and type |
| 17 | compute_late_minutes (line 35) | Doesn't handle overnight shifts correctly per R1 | Wrong attendance date for overnight shifts | Added overnight shift logic in punch_in to determine correct date |
| 18 | compute_late_minutes (line 36) | Doesn't truncate to whole seconds before calculation | Violates R1 requirement | Added truncate_to_seconds() call before calculation |
| 19 | compute_late_minutes (line 37) | Grace period logic wrong (minutes > 10) | Should be "strictly more than 10 minutes" | Fixed to correctly implement R2: 09:40:00 -> 0, 09:40:01 -> 10 |
| 20 | compute_work_hours (line 41) | Uses Python's round() which uses banker's rounding | R4 requires half-up rounding | Changed to use Decimal with ROUND_HALF_UP |
| 21 | compute_overtime (line 46) | datetime.fromisoformat doesn't handle timezones | Creates naive datetime, incorrect for IST | Changed to parse_time_to_ist_datetime() with proper timezone handling |
| 22 | compute_overtime (line 47) | Doesn't enforce 30-minute minimum per R3 | Would count overtime < 30 minutes | Added check: only return minutes if >= 30 |
| 23 | compute_overtime | Doesn't handle overnight shifts correctly | Wrong shift_end datetime for overnight shifts | Added logic to determine correct date for shift_end |
| 24 | list_attendance (line 144) | Loads all documents into memory before sorting | Would be slow/fail at 100k records | Changed to sort in MongoDB with .sort() before limit |
| 25 | list_attendance (line 145) | Sorts in Python instead of MongoDB | Inefficient, loads all data | Moved sorting to MongoDB query |
| 26 | list_attendance (line 149) | Returns "id" field which shouldn't exist | API doesn't use MongoDB _id | Removed id field |
| 27 | list_attendance | Returns punch_in/punch_out as datetime | API contract requires epoch milliseconds | Added convert_doc_for_response() to convert |
| 28 | list_attendance | No validation that date_from <= date_to | Invalid range would return empty results | Added validation to raise 422 if date_from > date_to |
| 29 | list_attendance | No sorting by emp_code as secondary sort key | Results not fully sorted as required | Added secondary sort: ("date", DESCENDING), ("emp_code", ASCENDING) |
| 30 | General (line 22) | MongoClient not configured with tz_aware=True | PyMongo returns naive datetimes by default | Added tz_aware=True to MongoClient |
| 31 | General | No indexes created at startup | Queries would be slow at 100k records | Added create_indexes() at startup with all required indexes |
| 32 | General | Missing all new endpoints (punch-out, regularize, analytics, explain) | API contract incomplete | Implemented all missing endpoints per OpenAPI spec |

Also note anything you looked at and decided was **not** a defect, and why:

- The basic structure of the starter code (separating helpers, models, and endpoints) was reasonable and I kept this pattern.
- The use of Pydantic models for request validation was correct, I just extended it with proper validators.
- The MongoDB connection using environment variables was correct, I kept this approach.
