# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints):

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` (L72-74) | Returns static `{"status": "ok"}` without pinging MongoDB. | Stop MongoDB service or misconfigure URI; endpoint still returns 200 instead of 503. | Added `db.command("ping")`; returns 200 when MongoDB responds, raises HTTPException 503 on connection failure. |
| 2 | `compute_late_minutes` (L33-37) | `punch_in.replace(hour=h, minute=m)` mixes UTC punch times with IST shift hours; also `minutes > 10` fails on 10 min 1 sec. | Punch-in at 09:40:01 for a 09:30 shift: `total_seconds` is 601s (10 min 1 sec), `int(601/60)` is 10, `10 > 10` is False, returning 0 instead of 10. Also UTC datetimes produce negative offsets. | Converted punch instant to IST; calculated exact elapsed seconds from shift start; checked `diff_seconds > 600`; computed floor minutes `int(diff_seconds // 60)`. |
| 3 | `compute_work_hours` (L40-41) | Uses Python built-in `round()`, which applies banker's rounding (round half-to-even) instead of round half-up (R4). | E.g., 7650 elapsed seconds = 2.125 hours. Built-in `round(2.125, 2)` gives `2.12`, failing the contract requirement of `2.13`. | Implemented `Decimal` division with `ROUND_HALF_UP` to two decimal places. |
| 4 | `compute_overtime` (L44-47) | Does not handle overnight shifts; ignores the 30-minute threshold (R3); mixes naive datetime with timezone-aware datetime. | For an overnight shift (22:00-06:00) punching out at 06:40 IST next day, calculates ~1480 min overtime; grants overtime for 5 min (< 30 min threshold); raises `TypeError: can't subtract offset-naive and offset-aware datetimes`. | Advanced shift end to the next calendar day when `shift_end <= shift_start`; converted both datetimes to IST; only counted floor overtime minutes when >= 30, otherwise 0. |
| 5 | `create_employee` (L78-80) | Check-then-insert race condition (`find_one` then `insert_one`); missing unique database index. | Two concurrent requests with the same `emp_code` both pass `find_one` and create duplicate documents. | Created a unique index on `employees.emp_code` at startup; caught PyMongo `DuplicateKeyError` and returned HTTP 409. |
| 6 | `create_employee` (L81-85) | `created_at` stored as naive local datetime via `datetime.now()` and returned as datetime object or leaking internal fields. | Response does not return integer epoch milliseconds; breaks `EpochMillis` schema. | Set `created_at` as UTC datetime truncated to whole seconds; serialized to integer epoch milliseconds in API response. |
| 7 | `EmployeeIn` / validation (L53-61) | Missing shift validation (`shift_start != shift_end`) and regex constraints on `emp_code` / email. | Creating an employee with identical `shift_start` and `shift_end` succeeds instead of failing with 422. | Added Pydantic field validators with pattern `^EMP\d{4,6}$` and a model validator ensuring `shift_start != shift_end`. |
| 8 | `list_employees` (L93-94) | Off-by-one pagination bug (`skip = page * page_size`); `total` counts entire collection ignoring `department` filter. | Requesting `page=1&page_size=20` sets `skip=20`, skipping the first 20 records; filtering by `department` returns total count of all departments. | Fixed skip calculation to `(page - 1) * page_size`; filtered `count_documents(q)`; sorted by `emp_code` ascending. |
| 9 | `punch_in` (L101-105) | Unhandled `None` employee lookup causes unhandled 500 error; check-then-insert race condition on date. | Non-existent `emp_code` throws `TypeError: 'NoneType' object is not subscriptable` (HTTP 500) instead of 404; concurrent punch-ins create duplicate logs. | Validated employee existence (returning 404); created unique index on `(emp_code, date)` and caught `DuplicateKeyError` to return 409. |
| 10 | `punch_in` (L102-103) | Local time conversion; fails to compute attendance date across midnight for overnight shifts (R1). | Punch-in at 01:00 IST for a 22:00-06:00 shift incorrectly records attendance for the current calendar day instead of the previous day. | Implemented `compute_attendance_date` with IST overnight shift logic (prior calendar day if IST time < `shift_end`). |
| 11 | `punch_in` (L106-121) | Injects MongoDB ObjectId as `id: str(...)`; accepts invalid statuses like `LEAVE` or `ABSENT`. | Response contains unexpected `id` property not in OpenAPI schema; client can punch in as `LEAVE`. | Validated status to only allow `PRESENT`, `WFH`, `ON_DUTY`; conformed output to `AttendanceRecord` schema with epoch ms and no internal IDs. |
| 12 | `list_attendance` (L144-150) | In-memory `find(q)` loading the entire collection into memory; in-memory sorting; lacks validation for `date_from > date_to`. | Querying attendance on a 100k-record collection causes massive memory consumption and slow response times; `date_from > date_to` returns 200 instead of 422. | Validated `date_from <= date_to` with 422; executed `.sort([("date", -1), ("emp_code", 1)]).skip(...).limit(...)` at the database level backed by compound index. |

---

### Behaviors Examined and Found Correct

1. **HTTP Status Code 201 on Creation**:
   `POST /employees` and `POST /attendance/punch-in` return status code 201 upon successful creation, which matches the OpenAPI contract.
2. **Default Pagination Parameters**:
   The default values `page = 1` and `page_size = 20` correctly align with Rule R10 and OpenAPI parameter defaults.
3. **Pydantic Request Parsing**:
   FastAPI automatic request body deserialization and 422 validation error handling structure adhere to FastAPI standard behavior required by the contract.
