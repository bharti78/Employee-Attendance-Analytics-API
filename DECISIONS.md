# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** I created unique index on (emp_code, date) for attendance_logs to prevent duplicate punch-ins and enable race-safe concurrent requests. Also indexed emp_code alone for employee lookups, date for date-range queries, and department for filtering. On employees, unique index on emp_code enforces uniqueness, plus department and joined_on for analytics queries. I considered a compound index on (date, emp_code) for the attendance list endpoint's sort order, which I also created.

2. **Punch-in race.** When two identical punch-ins arrive simultaneously, both try to insert with the same (emp_code, date). The unique index guarantees only one succeeds (gets 201). The other hits a duplicate key error, which I catch and convert to a 409 response. This is atomic at the database level, so no check-then-insert race condition.

3. **Ties.** My leaderboard uses Python-based dense ranking after fetching sorted results from MongoDB. When employees tie at the cutoff, all of them are included because I filter by rank <= limit after ranking. Tied rows are sorted by emp_code ascending as required.

4. **Headcount.** Department summary first queries employees to count those with joined_on <= month end (R9), storing headcount per department. Then it aggregates attendance logs and merges the results, ensuring departments with zero logs still appear with their headcount.

5. **One thing I would change** for 100x data: Add read replicas to distribute query load, and consider sharding by emp_code or date to keep collections manageable. Also add caching for frequently accessed analytics.
