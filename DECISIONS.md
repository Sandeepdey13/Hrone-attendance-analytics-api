# Design Decisions

1. **Indexes.** Unique `employees.emp_code` supports identity lookups. `joined_on` and `(department, joined_on)` support headcount and department summaries; `(department, emp_code)` supports filtered employee lists. Unique `(emp_code, date)` prevents duplicate attendance and supports employee-month queries and joins. Date/employee, status/date, and date/late indexes support attendance pages and leaderboard filtering. `(emp_code, punch_in)` finds the latest punch-in. Email is not indexed because the contract has no email lookup.
2. **Punch-in race.** Both requests may read the employee, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB rejects the other insert with `DuplicateKeyError`; the API maps it to 409. No pre-check is relied on for correctness.
3. **Ties.** `$rank` assigns tied totals the same rank and skips the next rank. The limit filters on rank after ranking, so every employee tied at or before the cutoff is returned, ordered by employee code.
4. **Headcount.** The summary starts from employees who joined by month end, then looks up logs. Employees with no matching logs remain in the employee stream and contribute to headcount.
5. **100x data.** I would measure explain plans and workload first, then consider pre-aggregated daily summaries and keyset pagination to reduce repeated scans and deep skips.
