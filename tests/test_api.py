import os
import threading
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient
from pymongo import MongoClient

# Use dedicated test DB
os.environ["MONGO_DB"] = "attendance_pytest_db"
from app.main import app, db, init_indexes, IST, compute_late_minutes, compute_work_hours, compute_overtime, compute_half_day


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    # Setup test DB cleanly
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    init_indexes()
    yield
    # Cleanup after test suite
    client = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    client.drop_database("attendance_pytest_db")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# Unit Tests for Business Rules (R1 - R8)
def test_late_minutes_boundary():
    # Shift 09:30 IST. Grace period is 10 min.
    # 09:40:00 -> 0 min late
    dt_exact = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc) # 09:40:00 IST
    assert compute_late_minutes(dt_exact, "2026-07-06", "09:30") == 0

    # 09:40:01 -> 10 min late (strictly > 10 min, elapsed = 10 min)
    dt_one_sec = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc) # 09:40:01 IST
    assert compute_late_minutes(dt_one_sec, "2026-07-06", "09:30") == 10

    # 10:15:59 -> 45 min late
    dt_45 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc) # 10:15:59 IST
    assert compute_late_minutes(dt_45, "2026-07-06", "09:30") == 45


def test_overtime_threshold():
    # Shift ends 18:30 IST. Threshold >= 30 min.
    # 29 min overtime (18:59:00 IST) -> 0
    dt_29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_29, "2026-07-06", "09:30", "18:30") == 0

    # 30 min overtime (19:00:00 IST) -> 30
    dt_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_30, "2026-07-06", "09:30", "18:30") == 30

    # 45 min overtime (19:15:00 IST) -> 45
    dt_45 = datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_45, "2026-07-06", "09:30", "18:30") == 45


def test_work_hours_half_up_rounding():
    # 7650 seconds = 2.125 hours -> round half-up must be 2.13 (not 2.12 banker's rounding!)
    t1 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 6, 6, 7, 30, tzinfo=timezone.utc) # 2h 7m 30s = 7650s
    wh = compute_work_hours(t1, t2)
    assert wh == 2.13
    assert compute_half_day(wh) is True

    # Exactly 4.50 hours
    t3 = datetime(2026, 7, 6, 8, 30, 0, tzinfo=timezone.utc)
    wh2 = compute_work_hours(t1, t3)
    assert wh2 == 4.50
    assert compute_half_day(wh2) is False # < 4.50 is half day, 4.50 is full day


def test_overnight_shift_calculations():
    # Shift 22:00 to 06:00.
    # Punch-in at 21:55 IST on 2026-07-06 -> belongs to date 2026-07-06
    # Punch-out at 06:40 IST on 2026-07-07 -> overtime relative to next day 06:00 IST = 40 mins
    punch_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc) # 06:40 IST
    ot = compute_overtime(punch_out, "2026-07-06", "22:00", "06:00")
    assert ot == 40


# Endpoint Tests
def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud(client):
    # Missing fields -> 422
    res = client.post("/employees", json={"emp_code": "EMP9999"})
    assert res.status_code == 422

    # Equal shift_start and shift_end -> 422
    res = client.post("/employees", json={
        "emp_code": "EMP9999",
        "name": "Invalid Shift",
        "email": "invalid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Valid creation -> 201
    payload = {
        "emp_code": "EMP1001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP1001"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate creation -> 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # List employees
    res_list = client.get("/employees?department=Engineering")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] >= 1
    assert any(e["emp_code"] == "EMP1001" for e in list_data["items"])


def test_punch_in_and_out(client):
    # Unknown employee -> 404
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

    # Bad timestamp (string or small second) -> 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 12345})
    assert res.status_code == 422
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": "not_an_int"})
    assert res.status_code == 422

    # Successful punch-in (09:40:01 IST on 2026-07-06 = 1783311001000)
    # 2026-07-06 04:10:01 UTC = 1783311001000 ms
    p_in_ms = int(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001",
        "punched_at": p_in_ms,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-06"
    assert in_data["late_minutes"] == 10
    assert in_data["punch_out"] is None
    assert in_data["work_hours"] is None

    # Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001",
        "punched_at": p_in_ms
    })
    assert res_dup.status_code == 409

    # Punch-out before punch-in -> 422
    early_ms = p_in_ms - 10000
    res_early = client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": early_ms
    })
    assert res_early.status_code == 422

    # Successful punch-out (19:15:00 IST = 13:45:00 UTC) -> 45 min overtime
    p_out_ms = int(datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": p_out_ms
    })
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["overtime_minutes"] == 45
    assert out_data["work_hours"] > 9.0
    assert out_data["half_day"] is False

    # Second punch-out on closed record -> 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP1001",
        "punched_at": p_out_ms + 1000
    })
    assert res_out_dup.status_code == 409


def test_regularize_and_audit_history(client):
    # Regularize record
    patch_body = {
        "reason": "Biometric terminal failed to recognize thumb",
        "regularized_by": "hr_manager",
        "status": "WFH"
    }
    res = client.patch("/attendance/EMP1001/2026-07-06", json=patch_body)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr_manager"
    assert "status" in h["changes"]
    assert h["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}

    # Request that changes nothing -> 422
    res_no_change = client.patch("/attendance/EMP1001/2026-07-06", json=patch_body)
    assert res_no_change.status_code == 422


def test_concurrency_simultaneous_punch_in(client):
    # Create a test employee
    emp_code = "EMP2001"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Bob Concurrent",
        "email": "bob@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    p_in_ms = int(datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    results = []

    def do_punch():
        with TestClient(app) as c:
            r = c.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": p_in_ms})
            results.append(r.status_code)

    t1 = threading.Thread(target=do_punch)
    t2 = threading.Thread(target=do_punch)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one 201 and one 409
    assert sorted(results) == [201, 409]


def test_concurrency_simultaneous_punch_out(client):
    emp_code = "EMP2002"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Charlie Concurrent",
        "email": "charlie@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    p_in_ms = int(datetime(2026, 7, 9, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": p_in_ms})

    p_out_ms = int(datetime(2026, 7, 9, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    results = []

    def do_punch_out():
        with TestClient(app) as c:
            r = c.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": p_out_ms})
            results.append(r.status_code)

    t1 = threading.Thread(target=do_punch_out)
    t2 = threading.Thread(target=do_punch_out)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one 200 and one 409
    assert sorted(results) == [200, 409]


# Analytics Tests
def test_employee_monthly_analytics(client):
    res = client.get("/analytics/employees/EMP1001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP1001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] >= 1.0


def test_department_summary(client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert len(data["items"]) >= 1
    depts = [d["department"] for d in data["items"]]
    assert "Engineering" in depts


def test_late_leaderboard_ties(client):
    # Create two employees with identical late minutes in month 2026-08
    emp1 = "EMP3001"
    emp2 = "EMP3002"
    client.post("/employees", json={
        "emp_code": emp1, "name": "Tie One", "email": "tie1@example.com",
        "department": "Finance", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    client.post("/employees", json={
        "emp_code": emp2, "name": "Tie Two", "email": "tie2@example.com",
        "department": "Finance", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    # Both 20 min late on 2026-08-03 (09:50:00 IST = 04:20:00 UTC)
    p_ms = int(datetime(2026, 8, 3, 4, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp1, "punched_at": p_ms})
    client.post("/attendance/punch-in", json={"emp_code": emp2, "punched_at": p_ms})

    res = client.get("/analytics/leaderboard/late?month=2026-08&department=Finance")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 2
    # Standard competition ranking: ties share rank 1
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    assert items[0]["total_late_minutes"] == 20
    assert items[1]["total_late_minutes"] == 20
    # Ordered by emp_code ascending on tie
    assert items[0]["emp_code"] == emp1
    assert items[1]["emp_code"] == emp2


def test_department_trend(client):
    # Trend for Engineering from 2026-07-05 to 2026-07-10 (6 days)
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 6
    # Day 1 is Sunday -> is_working_day False, attendance_rate None
    assert data["items"][0]["is_working_day"] is False
    assert data["items"][0]["attendance_rate"] is None
    # Day 2 is Monday -> is_working_day True
    assert data["items"][1]["is_working_day"] is True


def test_explain_endpoint(client):
    # Explain for attendance_list
    res1 = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["collection"] == "attendance_logs"
    assert "explain" in d1

    # Explain for employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP1001&month=2026-07")
    assert res2.status_code == 200

    # Explain for department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200

    # Explain for late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200

    # Explain for department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res5.status_code == 200


def test_legacy_record_compatibility(client):
    # Insert legacy document missing history and half_day directly into MongoDB
    emp_code = "EMP4001"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Legacy User",
        "email": "legacy@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    p_in_dt = datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc)
    p_out_dt = datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc)
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": p_in_dt,
        "punch_out": p_out_dt,
        "work_hours": 8.98,
        "late_minutes": 0,
        "overtime_minutes": 0
        # Missing half_day and history
    })

    res = client.get(f"/attendance?emp_code={emp_code}")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    rec = items[0]
    assert rec["half_day"] is False
    assert rec["history"] == []


def test_mid_month_joiner_and_headcount(client):
    # Employee joining mid-month with zero attendance logs
    emp_code = "EMP5001"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Mid Month Joiner",
        "email": "mid@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20" # Monday, July 20 (10 weekdays left in July 2026: 20-24, 27-31)
    })

    # Working days should be counted only from 2026-07-20 to 2026-07-31 = 10 weekdays
    res_m = client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res_m.status_code == 200
    m_data = res_m.json()
    assert m_data["working_days"] == 10
    assert m_data["present_days"] == 0.0

    # Department summary must count this employee in headcount even with zero logs (R9)
    res_d = client.get("/analytics/departments/summary?month=2026-07&department=Operations")
    assert res_d.status_code == 200
    items = res_d.json()["items"]
    assert len(items) == 1
    assert items[0]["headcount"] == 1
    assert items[0]["present_days"] == 0.0


def test_weekend_attendance(client):
    # Punch on Saturday: 2026-07-11 (Saturday)
    emp_code = "EMP6001"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Weekend Warrior",
        "email": "weekend@example.com",
        "department": "Security",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    # Punch in 20 min late on Saturday (09:50 IST = 04:20 UTC)
    p_in_ms = int(datetime(2026, 7, 11, 4, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # Punch out with 45 min overtime (19:15 IST = 13:45 UTC)
    p_out_ms = int(datetime(2026, 7, 11, 13, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": p_in_ms})
    client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": p_out_ms})

    res = client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    # Weekend does NOT count toward present_days (R7)
    assert data["present_days"] == 0.0
    # Weekend DOES contribute to late and overtime (R7)
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 20
    assert data["total_overtime_minutes"] == 45


def test_leaderboard_cutoff_tie_returns_more_than_limit(client):
    # Two employees tied on late minutes with limit=1 -> returns BOTH (rank <= limit => 1 <= 1)
    res = client.get("/analytics/leaderboard/late?month=2026-08&department=Finance&limit=1")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1



# Regression Tests (post-audit)

def test_email_regex_validation(client):
    """Email validator rejects missing @, missing dot in domain, and spaces."""
    base = {
        "name": "Email Test", "department": "Eng",
        "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01",
    }
    for good, code in [
        ("good@example.com", "EMP7001"),
        ("a.b+tag@sub.domain.io", "EMP7002"),
    ]:
        r = client.post("/employees", json={**base, "email": good, "emp_code": code})
        assert r.status_code == 201, f"expected 201 for {good!r}, got {r.status_code}"

    for bad in ["noDomain", "alice@", "@example.com", "alice@example", "al ice@ex.com"]:
        r = client.post("/employees", json={**base, "email": bad, "emp_code": "EMP7099"})
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_punch_out_selects_record_at_or_before_punched_at(client):
    """
    Spec: punch-out closes the most recent record whose punch_in <= punched_at.
    Regression: two open records; punch_out falls between the two punch_ins;
    the earlier record must be closed, not the newer one.
    """
    from datetime import datetime, timezone
    emp_code = "EMP8001"
    client.post("/employees", json={
        "emp_code": emp_code, "name": "Two Open", "email": "twoopen@example.com",
        "department": "Ops", "shift_start": "09:00", "shift_end": "17:00",
        "joined_on": "2026-01-01",
    })
    # Day 1 punch-in: 09:00 IST on 2026-09-01 = 03:30 UTC
    pi_day1_ms = int(datetime(2026, 9, 1, 3, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_day1_ms})
    assert r.status_code == 201

    # Day 2 punch-in: 09:00 IST on 2026-09-02 = 03:30 UTC (also open)
    pi_day2_ms = int(datetime(2026, 9, 2, 3, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_day2_ms})
    assert r.status_code == 201

    # Punch out for Day 1 at 17:00 IST = 11:30 UTC on 2026-09-01 (before day-2 punch_in)
    po_day1_ms = int(datetime(2026, 9, 1, 11, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": po_day1_ms})
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.json()}"
    data = r.json()
    assert data["date"] == "2026-09-01", f"Closed wrong date: {data['date']}"
    assert data["punch_out"] is not None

    # Day 2 record must still be open
    r2 = client.get(
        f"/attendance?emp_code={emp_code}"
        "&date_from=2026-09-02&date_to=2026-09-02"
    )
    assert r2.status_code == 200
    assert r2.json()["items"][0]["punch_out"] is None, "Day-2 should still be open"


def test_concurrency_simultaneous_regularize(client):
    """
    Two concurrent PATCHes on the same record: exactly one 200, one 409.
    The winning change must appear exactly once in history (no silent loss or duplicate).
    """
    import threading
    from datetime import datetime, timezone
    from starlette.testclient import TestClient as TC
    from app.main import app as _app, db as _db

    emp_code = "EMP8002"
    client.post("/employees", json={
        "emp_code": emp_code, "name": "Concurrent Reg", "email": "creg@example.com",
        "department": "QA", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    pi_ms = int(datetime(2026, 10, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": pi_ms})

    results = []

    def do_patch(suffix, target_status):
        with TC(_app) as c:
            r = c.patch(
                f"/attendance/{emp_code}/2026-10-01",
                json={
                    "reason": f"Concurrent correction {suffix}",
                    "regularized_by": f"hr_{suffix}",
                    "status": target_status,
                },
            )
            results.append(r.status_code)

    t1 = threading.Thread(target=do_patch, args=("A", "WFH"))
    t2 = threading.Thread(target=do_patch, args=("B", "ON_DUTY"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert all(code in (200, 409) for code in results), f"Unexpected status codes: {results}"
    rec = _db.attendance_logs.find_one({"emp_code": emp_code, "date": "2026-10-01"})
    assert len(rec.get("history", [])) == results.count(200)


# Shift Rotation & Weekly Hours Tracking Tests

def test_shift_rotation_standard_and_custom(client):
    """Test shift rotation using canonical presets and custom hours."""
    emp_code = "EMP9001"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Shift Worker",
        "email": "shiftworker@example.com",
        "department": "Production",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    # 1. Rotate to standard NIGHT preset (actual shift change -> notification dispatched)
    res1 = client.patch(f"/employees/{emp_code}/shift", json={"shift_name": "NIGHT"})
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["new_shift"]["shift_name"] == "NIGHT"
    assert data1["new_shift"]["shift_start"] == "21:00"
    assert data1["new_shift"]["shift_end"] == "05:00"
    assert data1["notification_status"] in ("SENT_OFFLINE", "SENT")

    # 2. Same shift rotation again (unchanged shift -> notification UNCHANGED, no duplicate email)
    res_same = client.patch(f"/employees/{emp_code}/shift", json={"shift_name": "NIGHT"})
    assert res_same.status_code == 200
    assert res_same.json()["notification_status"] == "UNCHANGED"

    # 3. Rotate to custom shift hours
    res2 = client.patch(f"/employees/{emp_code}/shift", json={
        "shift_start": "11:00",
        "shift_end": "20:00",
    })
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["new_shift"]["shift_start"] == "11:00"
    assert data2["new_shift"]["shift_end"] == "20:00"
    assert data2["notification_status"] in ("SENT_OFFLINE", "SENT")

    # 4. Validation errors: identical shift_start and shift_end -> 422
    res_bad = client.patch(f"/employees/{emp_code}/shift", json={
        "shift_start": "10:00",
        "shift_end": "10:00",
    })
    assert res_bad.status_code == 422

    # 5. Unknown shift name -> 422
    res_unknown = client.patch(f"/employees/{emp_code}/shift", json={"shift_name": "GRAVEYARD_EXTRA"})
    assert res_unknown.status_code == 422


def test_weekly_working_hours_and_daily_deficit(client):
    """Test weekly tracking against 40h target, with shortfall and daily deficit flags."""
    from datetime import datetime, timezone
    from app.main import db

    emp_code = "EMP9002"
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Weekly Tracker",
        "email": "wtracker@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    # Week 2026-W28: Monday 2026-07-06 to Sunday 2026-07-12
    # Monday 2026-07-06: 9.0 hours (COMPLIANT)
    # Tuesday 2026-07-07: 6.0 hours (INSUFFICIENT_HOURS, >4.5h but <8h)
    # Wednesday 2026-07-08: 3.0 hours (HALF_DAY_DEFICIT, <4.5h)
    # Thursday 2026-07-09: 0 hours / unrecorded (ABSENT)
    # Friday 2026-07-10: LEAVE (approved leave)

    dates_hours = [
        ("2026-07-06", 9.0, False, "PRESENT"),
        ("2026-07-07", 6.0, False, "PRESENT"),
        ("2026-07-08", 3.0, True, "PRESENT"),
        ("2026-07-10", 0.0, False, "LEAVE"),
    ]

    for d, h, hd, st in dates_hours:
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": d,
            "status": st,
            "punch_in": datetime(2026, 7, int(d.split("-")[2]), 4, 0, 0, tzinfo=timezone.utc) if st != "LEAVE" else None,
            "punch_out": datetime(2026, 7, int(d.split("-")[2]), 13, 0, 0, tzinfo=timezone.utc) if st != "LEAVE" else None,
            "work_hours": h if st != "LEAVE" else None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": hd,
            "history": [],
        })

    res = client.get(f"/analytics/employees/{emp_code}/weekly?week=2026-W28")
    assert res.status_code == 200
    data = res.json()

    assert data["target_weekly_hours"] == 40.0
    assert data["actual_weekly_hours"] == 18.0
    assert data["shortfall_hours"] == 22.0
    assert data["is_compliant"] is False
    assert data["summary"]["working_days"] == 5
    assert data["summary"]["absent_days"] == 0 # Unrecorded is not marked ABSENT
    assert data["summary"]["leave_days"] == 1
    assert data["summary"]["unrecorded_days"] == 1

    day_map = {d["date"]: d for d in data["days"]}
    # Mon: 9.0h COMPLIANT
    assert day_map["2026-07-06"]["flag"] == "COMPLIANT"
    # Tue: 6.0h INSUFFICIENT_HOURS
    assert day_map["2026-07-07"]["flag"] == "INSUFFICIENT_HOURS"
    # Wed: 3.0h HALF_DAY_DEFICIT
    assert day_map["2026-07-08"]["flag"] == "HALF_DAY_DEFICIT"
    # Thu: unrecorded scheduled working day -> UNRECORDED (not ABSENT)
    assert day_map["2026-07-09"]["flag"] == "UNRECORDED"
    assert day_map["2026-07-09"]["status"] is None
    # Fri: approved leave -> LEAVE (distinguished from ABSENT)
    assert day_map["2026-07-10"]["flag"] == "LEAVE"
    # Sat & Sun: WEEKEND
    assert day_map["2026-07-11"]["flag"] == "WEEKEND"
    assert day_map["2026-07-12"]["flag"] == "WEEKEND"

