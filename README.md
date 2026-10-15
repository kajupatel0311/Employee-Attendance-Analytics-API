# Employee Attendance & Analytics API

A robust backend service for employee attendance tracking, regularization, and multi-dimensional analytics built with **FastAPI**, **MongoDB 7.0**, and **PyMongo**, following the official HROne hiring assignment contract.

---

## 1. Prerequisites

- **Python**: 3.11 or newer (tested with Python 3.13)
- **MongoDB**: 6.0 or newer (tested with MongoDB 7.0)

---

## 2. Environment Variables

The application reads configuration dynamically from environment variables, giving OS-level environment variables precedence over any local `.env` file:

| Variable | Description | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection URI string | `mongodb://localhost:27017` |
| `MONGO_DB` | Target MongoDB database name | `attendance_db` |

*Note: No credentials or connection strings are hardcoded in the codebase.*

---

## 3. Installation & Local Setup

1. **Clone the repository and enter the project directory:**
   ```bash
   git clone <repo-url>
   cd candidate_kit
   ```

2. **Create and activate a virtual environment:**
   ```bash
   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate

   # Windows (PowerShell)
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **(Optional) Seed sample data:**
   ```bash
   python sample_seed.py
   ```

---

## 4. Starting the Application

The application entrypoint is located entirely in `app/main.py`:

```bash
uvicorn app.main:app --port 8000
```

Upon startup, the application lifecycle:
- Pings MongoDB to verify database readiness.
- Automatically and idempotently creates all required unique and query indexes.

Interactive Swagger documentation is accessible at `http://localhost:8000/docs`.

---

## 5. Running the Test Suite

Run the automated test suite covering all business rules (R1–R10), concurrency, overnight shifts, legacy documents, and explain plans:

```bash
python -m pytest -v tests/test_api.py
```

---

## 6. Architecture & Implementation Highlights

- **All Application Code in `app/main.py`**: Clean, modular structure keeping models, helpers, endpoints, and pipelines in a single file per instructions.
- **R1–R10 Rule Compliance**:
  - **R1**: Integer epoch milliseconds across API boundaries; truncated to whole seconds; IST calendar dates with proper overnight shift handling.
  - **R2**: Lateness strictly after 10:00 grace period (floor minutes from shift start).
  - **R3**: Overtime floor minutes calculated relative to shift end (and next calendar day for overnight shifts) only when >= 30 minutes.
  - **R4 & R8**: Work hours and reported rates rounded using `Decimal` half-up rounding (`ROUND_HALF_UP`), avoiding float/banker's rounding discrepancies.
  - **R5**: Half-day set to true when rounded work hours are strictly below 4.50.
  - **R6 & R7**: Weekday-only presence calculation (`PRESENT`, `WFH`, `ON_DUTY`); mid-month joiner working days calculated from `joined_on`.
  - **R9**: Department headcount includes employees who joined on or before month end, even those with zero attendance logs.
  - **R10**: 1-based pagination with total reflecting applied filters.
- **Concurrency & Atomicity**:
  - Simultaneous punch-ins: Guarded by database unique index on `(emp_code, date)` yielding exactly one 201 and 409 for conflicts.
  - Simultaneous punch-outs: Atomic conditional update with `find_one_and_update(..., punch_out=None)` yielding exactly one 200 and 409 for competing updates.
  - Regularizations: Optimistic versioning on history length preventing silent overwrites of audit records.
- **Database-Side Aggregations**:
  - Employee monthly summary, department summary, late leaderboard (`$rank` standard competition ranking), and daily department trend with gap-filling (`$densify`) and 7-day moving averages (`$setWindowFields`) are executed entirely within MongoDB aggregation pipelines.
- **Explain Endpoint (`GET /admin/explain/{endpoint}`)**:
  - Reuses shared query/pipeline builders with `executionStats`, validated against database indexes (`IXSCAN`).
- **Shift Rotation & Presets (`PATCH /employees/{emp_code}/shift`)**:
  - Supports standard company shift presets (`GENERAL`, `MORNING`, `EVENING`, `NIGHT` overnight) or custom hours without modifying historical attendance logs.
  - Automatically dispatches shift-update email notifications (offline testable via outbox, delivered via SMTP when `SMTP_HOST` is configured).
- **Weekly Tracking & Deficit Analysis (`GET /analytics/employees/{emp_code}/weekly`)**:
  - Tracks compliance against weekly 40-hour targets, calculating shortfalls and auditing daily compliance (`COMPLIANT`, `INSUFFICIENT_HOURS`, `HALF_DAY_DEFICIT`, `ABSENT`, `WEEKEND`) without mutating official records.
- **Real-Time Daily Coordination Summary (`GET /analytics/daily-summary`)**:
  - Real-time pulse providing HR with headcount counts for present, late, on-duty, WFH, on-leave, and unrecorded employees, complete with shift-mapped roster details for daily operational planning.

---

## 7. Known Limitations & Trade-Offs

- **No Dedicated Caching**: Aggregations are calculated directly on MongoDB. For 100x datasets (~10M documents), caching or incremental rollups via Change Streams would be required for real-time responsiveness.
- **No Role-Based Access Control**: In accordance with assignment scope, authentication and authorization are omitted; `regularized_by` is passed as plain text.
- **Performance at 100,000+ Records**: While all queries are backed by compound indexes (`(emp_code, date)`, `(date, emp_code)`, `(joined_on, 1)`), real-time aggregation across 100k+ unpartitioned documents remains bounded by MongoDB server RAM and query working sets.
