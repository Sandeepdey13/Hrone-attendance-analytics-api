# Design decisions

1. **Indexes.** `employees.emp_code` is unique to prevent duplicate identities. `department + joined_on` supports department headcount and month-wide eligibility queries. Attendance uses unique `emp_code + date` to enforce one record per employee-day, `date + emp_code` for date listings, `emp_code + punch_in` for the latest eligible punch, and `date + status + emp_code` for filters. I would avoid a standalone `status` index because it has low selectivity and does not help the main sort.

2. **Punch-in race.** Both requests may initially see no record, but the unique database index is authoritative. MongoDB accepts one insert; the other receives a duplicate-key error and the API returns 409. This avoids relying on check-then-insert.

3. **Ties.** The leaderboard ranks by total late minutes before sorting tied rows by employee code. Competition ranking assigns the same rank to ties and skips the next rank. Since `limit` filters rank, all employees tied at the cutoff are included.

4. **Headcount.** The department summary starts from employees who joined by month end, then looks up each person's logs. People without logs still count.

5. **100x scale.** I would inspect query plans first, then consider daily aggregates and cursor pagination.
