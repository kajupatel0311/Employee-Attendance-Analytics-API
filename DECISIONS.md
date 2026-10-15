# Technical Design Decisions

This document outlines the architecture, concurrency controls, data integrity guarantees, and scaling strategies implemented in the Attendance & Analytics API.

---

### 1. Indexing Strategy

MongoDB collections are indexed based on query patterns, sorting semantics, and cardinality:

- **`employees.emp_code` (Unique)**: Guarantees employee identity integrity at the database engine level. Prevents race conditions during employee creation where concurrent requests could slip through an application-level check.
- **`employees.(department, joined_on)` & `employees.joined_on`**: Covers department filtering and headcount evaluation during monthly aggregations (`joined_on <= month_end`). Indexing `joined_on` allows the database planner to use an index seek rather than scanning the entire employee table.
- **`attendance_logs.(emp_code, date)` (Unique compound)**: Serves as the primary operational constraint. Each employee can only have one attendance log per calendar date. It also powers point lookups and updates on `/attendance/{emp_code}/{date}`.
- **`attendance_logs.(date DESC, emp_code ASC)`**: Supports paginated log listing (`GET /attendance`) and date-range queries. By matching both the filter order and sort direction, MongoDB satisfies the query with an index scan (`IXSCAN`) without performing an in-memory sort.
- **`attendance_logs.(date, late_minutes)`**: Accelerates the monthly late leaderboard pipeline, filtering strictly for records with `late_minutes > 0` across the target date range.
- **`attendance_logs.(emp_code, punch_in DESC)`**: Optimizes the punch-out endpoint to instantly locate an employee's most recent open record without full-collection scans.

*Rejected alternatives:* We intentionally rejected creating an isolated single-field index on `status`. Attendance status has very low cardinality (5 values), which offers poor selectivity and incurs unnecessary write overhead during high-frequency punches.

---

### 2. Concurrency & Race Condition Handling

In attendance systems, race conditions often occur when workers punch in simultaneously at shift boundaries or submit multiple requests on unstable mobile networks. We avoid application-level "check-then-act" antipatterns:

- **Simultaneous Duplicate Punch-In**:
  Two concurrent punch-in requests for the same employee on the same date both issue `insert_one`. The database engine atomically processes one insert, while the competing write fails with a MongoDB `DuplicateKeyError` (code 11000) on the unique compound index `(emp_code, date)`. The API catches this error and returns a clean `409 Conflict`.
- **Simultaneous Punch-Out**:
  Closing an attendance record uses an atomic Compare-And-Swap (CAS) update via `find_one_and_update`:
  ```python
  db.attendance_logs.find_one_and_update(
      {"_id": record["_id"], "punch_out": None},
      {"$set": {"punch_out": punch_out_dt, "work_hours": ..., "overtime_minutes": ...}}
  )
  ```
  If two punch-out requests arrive at the same millisecond, only the first request matches `"punch_out": None`. The second request finds no matching document, avoiding duplicate updates and returning `409 Conflict`.
- **Concurrent Regularizations (Audit History Preservation)**:
  To prevent concurrent corrections from overwriting one another's audit history, the update query conditions on the exact array length of the current history:
  ```python
  {"_id": rec["_id"], "history": {"$size": current_history_len}}
  ```
  The update uses `$push` to append the new audit entry while setting modified fields. If another user submits an edit in parallel, the history size changes, the second operation fails atomically, and the API returns `409 Conflict` without dropping historical data.

---

### 3. Competition Ranking & Cutoff Ties

The late arrival leaderboard (`/analytics/leaderboard/late`) requires competition ranking (1224 ranking), where tied employees receive the same rank and skip subsequent ranks accordingly.

- In MongoDB, this is computed using `$setWindowFields` with the `$rank` operator ordered by `total_late_minutes` descending.
- The pipeline applies a match filter on `rank <= limit`. If multiple employees tie at the cutoff rank (for instance, three employees share rank 5 when the limit is 5), all tied candidates are preserved in the response.
- Deterministic secondary sorting on `emp_code` ascending ensures stable ordering across repeated calls.

---

### 4. Department Headcount & Zero-Log Preservation

Calculating department metrics requires accurate headcount figures, especially for mid-month joiners or employees on extended leave:

- Starting the aggregation from `attendance_logs` would omit employees who had zero punches during the month, artificially inflating calculated attendance rates.
- Instead, the aggregation pipeline begins from the `employees` collection:
  1. Filters by `joined_on <= month_end` to include all active personnel.
  2. Groups by department to establish the baseline headcount.
  3. Uses `$lookup` with an embedded pipeline into `attendance_logs` scoped to the current month.
  4. Applies `$ifNull` defaults so departments where no logs were recorded still return accurate headcounts with `0.0` present days.

---

### 5. Architectural Strategy for 100x Scale (~10M Records)

At 100,000 records, compound indexes and native aggregation pipelines execute in under 15ms. However, scaling to 10M+ attendance records requires architectural adjustments:

1. **Pre-Aggregated Daily Rollups via Change Streams**:
   Rather than executing cross-collection `$lookup` stages on live traffic, worker tasks listening to MongoDB Change Streams can maintain pre-aggregated daily summaries (`date`, `department`, `headcount`, `present_days`, `total_hours`). Monthly summaries and trends then query lightweight summary documents in $O(1)$ time.
2. **Time-Based Partitioning & Sharding**:
   Sharding `attendance_logs` using a compound shard key `(date, emp_code)` isolates older historical records from high-throughput daily punch traffic, keeping the active working set resident in memory.
3. **Read Replicas for Analytics**:
   Directing analytical queries (trends, leaderboards) to secondary replica set members prevents analytical report generation from contending with write locks on daily punch-ins.
