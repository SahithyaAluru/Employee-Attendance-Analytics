# Implementation decisions

1. **Indexes.** Unique employee codes and employee/date pairs enforce identity and prevent duplicate punches. Employee department/code supports sorted lists; joined-on and department/joined-on support headcount queries. Attendance date/code and status/date/code support pagination and monthly scans. Employee/punch-in finds the latest punch across midnight. I rejected indexing history because no endpoint searches it, and corrections would pay extra write cost. Explain and endpoints share builders and index hints.

2. **Punch-in race.** Both requests find the employee and calculate the attendance date. Each attempts an insert. MongoDB's unique employee/date index accepts exactly one; the other raises DuplicateKeyError, which becomes HTTP 409. There is no separate existence check for the attendance record.

3. **Ties.** MongoDB's rank window function assigns competition ranks using total late minutes alone. Filtering rank after ranking includes everyone tied at the cutoff. Employee code breaks ties only in the final display order.

4. **Headcount.** The summary starts from employees eligible by month-end. Each employee contributes one headcount, even when their indexed attendance lookup returns no statistics. Work-hour sums and counts produce a mean over records.

5. **More data.** I would maintain daily department summaries with correction-driven updates, then benchmark pagination and lookup plans before changing storage or sharding.
