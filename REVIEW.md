# Review of the starter `app/main.py`

| # | Where | Defect | How to notice it | Fix |
|---:|---|---|---|---|
| 1 | `compute_late_minutes` | Compares floored minutes to the grace period, so 09:40:01 on a 09:30 shift is not late. | Test 09:40:00 and 09:40:01. | Compare elapsed seconds strictly against 600, then floor elapsed minutes from shift start. |
| 2 | `compute_late_minutes` | Uses the punch timestamp's date and timezone directly; it cannot calculate an overnight shift's start correctly. | Test a 22:00–06:00 shift after midnight. | Convert instants to UTC/IST explicitly and use the stored attendance shift date. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even rather than half-up. | Test an interval whose hours round at an exact half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Uses the punch-out's calendar date and ignores overnight shifts. | Test a 22:00–06:00 shift ending the next morning. | Compute the shift end from the attendance date and advance one day for overnight shifts. |
| 5 | `compute_overtime` | Counts every positive minute rather than applying the minimum 30-minute threshold. | Test punch-out 29 minutes and 30 minutes after shift end. | Return zero below 30 minutes; otherwise floor the elapsed minutes. |
| 6 | Indexes / startup | No indexes are created, so uniqueness and query plans are not guaranteed. | Start with a seeded database and inspect indexes / explain plans. | Create idempotent unique and query-supporting indexes at startup. |
| 7 | `create_employee` | Check-then-insert can allow concurrent duplicate employee codes; there is no database-enforced uniqueness. | Send concurrent creates for the same `emp_code`. | Enforce a unique index and translate `DuplicateKeyError` to 409. |
| 8 | `create_employee` | `created_at` is a naive datetime and is returned as a datetime rather than epoch milliseconds. | Inspect the JSON response and compare it with the contract. | Store an explicit UTC instant and serialize instants as integer epoch milliseconds. |
| 9 | `EmployeeIn` | Does not validate employee code, email, date, shift format, or equal shift start/end. | Send malformed fields or identical shift times. | Add contract-level Pydantic validation. |
| 10 | `list_employees` | `skip=page * page_size` skips the first page and makes later pages incorrect. | Request page 1 with page size 2. | Use `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` counts all employees even when a department filter is applied, and results are not sorted. | Request a small department filter and compare `total`; repeat page requests. | Count with the same filter and sort by `emp_code`. |
| 12 | `punch_in` | An unknown employee makes `emp` null and can produce a 500 instead of 404. | Punch in with an unknown code. | Validate the employee exists before reading shift fields. |
| 13 | `punch_in` | Uses host-local `fromtimestamp`, doesn't truncate incoming instants, and calculates attendance date without IST/overnight rules. | Run under a non-IST host timezone and test an overnight shift. | Parse epoch milliseconds in UTC, truncate to seconds, then derive the IST attendance date. |
| 14 | `punch_in` | Check-then-insert is race-prone and accepts arbitrary status strings. | Send parallel punches or a status of `HOLIDAY`. | Use the unique `(emp_code, date)` index and constrain status to presence statuses. |
| 15 | `list_attendance` | Loads all matching records into Python before sorting and paging, which is not suitable for 100,000 rows. | Run against a large collection and measure memory/latency. | Count and page in MongoDB, using indexed filters and server-side sort/skip/limit. |
| 16 | `list_attendance` | Does not validate date ranges or tie-break sorting, and exposes an `id` derived from Mongo `_id`. | Reverse `date_from` / `date_to` or inspect a tied-date response. | Validate dates, sort by date descending then `emp_code` ascending, and omit Mongo IDs. |
| 17 | `list_attendance` and create/list responses | BSON timestamps are returned in their internal datetime form; nested history timestamps also need conversion. | Compare responses to the epoch-millisecond schema. | Serialize every API instant, including audit timestamps, as epoch milliseconds. |
| 18 | API coverage | Punch-out, regularization, all analytics routes, and explain are not implemented. | Call each operation in the OpenAPI contract. | Implement the missing endpoints, use aggregation pipelines for analytics, and provide explain output. |
| 19 | `/health` | Always returns 200 without checking MongoDB connectivity. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 when it is unavailable. |

### Reviewed and retained

The starter's general FastAPI/PyMongo approach is suitable for this task. Its use of a natural employee key (`emp_code`) is also correct; MongoDB `_id` should remain internal. The helper functions were useful starting points, but their time and rounding assumptions were not safe to reuse unchanged.
