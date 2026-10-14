# Verification — 13 October 2026

The configured MongoDB Atlas database `attendance_db` was reachable and contained
6 employees and 9 attendance records before and after testing. Existing attendance
data was preserved. Server version: MongoDB 8.0.34.

`tests/check_api.py --scale` passed all 114 checks in 75.3 seconds, using a uniquely
named disposable database that was removed after testing. The suite covers HTTP
validation, MongoDB fetching, legacy fields, timestamp corrections, audit history,
concurrent employee/punch creation and updates, overnight shifts, rounding,
headcount, ties, calendar gaps, moving averages and executionStats explain plans.
Every HTTP response in the suite is checked against the supplied OpenAPI response
schema. An independent Python reference calculation verifies monthly summaries,
department totals, leaderboard cutoffs and all 31 trend rows on reproducible
randomized data. Regression checks cover strict calendar dates and ignoring
client-supplied derived values as required by the contract.

After adding 100,000 records to the disposable database, all tested explain plans
contained IXSCAN and no COLLSCAN. Observed HTTP timings on this Atlas connection:

| Request | Seconds |
|---|---:|
| Attendance list, 20 records | 0.094 |
| Department summary, July 2026 | 0.609 |
| Late leaderboard, July 2026 | 0.172 |
| Department trend, 31 days, 1,000 employees | 0.750 |

Dependency consistency, source syntax/imports and Git whitespace checks passed.
The suite includes a simulated database outage check verifying HTTP 503 from
health. Index creation was run twice successfully; the unique indexes and Git
exclusions for credentials, virtual environments and editor settings were checked.

The refreshed API on port 8001 successfully served health, employees, attendance,
all four analytics endpoints and explain against the configured database. Employee
and attendance fetching returned all six and nine records respectively. Port 8000
is occupied by an older API process; use `http://localhost:8001/docs` for this run.

These results cover the supplied samples and explicit edge cases in the suite;
the assignment's hidden grader and MongoDB 6/7 were not available for testing.
