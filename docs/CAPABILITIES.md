# What IntentSQL can and cannot do

IntentSQL targets **SQLite**. It produces one read-only `SELECT` statement per question, and does not generate SQL for other dialects. Each capability below is exercised by `benchmarks/suites/features.json`, by the offline tests in `tests/`, or by both.

## Supported

### Basic queries

| Capability | Example |
|---|---|
| Projection, several columns | "List every store with its city" |
| Comparisons, BETWEEN, IN, LIKE (contains / starts / ends) | "products priced between 3 and 4", "suppliers whose names start with K" |
| AND / OR, negation | "gold-tier customers in Paris or Lyon", "orders that were not shipped" |
| NULL and empty values | "customers with no email address" (empty strings count as missing) |
| DISTINCT | "which different cities …" |
| ORDER BY (two keys), LIMIT, OFFSET | "the three most expensive products", "the second highest price" |
| Implicit values | "discontinued products" (a 0/1 flag), "male singers" |

### Relationships

| Capability | Example |
|---|---|
| Multi-hop joins over declared and inferred foreign keys | "regions with stores that sold Croissants" |
| Composite foreign keys | joined on every column |
| Self-joins (declared or inferred, e.g. `manager_id`, `SupervisorID`) | "employees who earn less than their manager", "each employee with their manager's name" |
| Several relationships between two tables | the relationship is chosen per request (e.g. source vs destination airport) |
| Ambiguous join routes | resolved by the relationship the request names, otherwise asked |
| No duplicated rows from filtering through related records | membership tests instead of joins |
| Outer joins | "including customers with no orders" |

### Aggregation

| Capability | Example |
|---|---|
| COUNT, SUM, AVG, MIN, MAX, COUNT(DISTINCT) | "how many different customers ordered in March 2024" |
| GROUP BY one or several keys, HAVING | "departments with an average salary above 5000" |
| Counting under fan-out | COUNT(DISTINCT key) when joins repeat rows |
| Nested aggregates | "average order value" (the average of per-order totals), "average number of orders per customer" (zero counts included) |
| Percentages | "what percentage of orders were cancelled" |
| Conditions on aggregates of subsets | "older than every engineer", "a higher resolution than English songs" |
| Comparisons with an average per record | "customers who placed more orders than the average customer" |

### Expressions

| Capability | Example |
|---|---|
| Products, differences, sums and ratios of stored columns | "revenue" = quantity × unit price, "total checking and savings balance" |
| Date differences | "the longest stay" (days between check-in and check-out) |
| Column vs column | "candidates with a lower support rate than oppose rate", "reviewers with the same name as the director" |

### Advanced queries

| Capability | Example |
|---|---|
| NOT IN / anti-joins | "customers who never ordered Espresso Beans" |
| Set operations | INTERSECT for "both A and B"; EXCEPT for "never", "have not" and "always" |
| Scalar and correlated subqueries | "above the average price", "above the average salary of their department" |
| Most / least common value | "documents with the least common structure" |
| Top-1 per group | correlated MAX/MIN, for example "the most expensive product in each category" |
| Top-N per group | ROW_NUMBER window, for example "the two cheapest products in each category" |

### Dates and time

| Capability | Example |
|---|---|
| Date comparisons and ranges | "orders placed after 2024-06-30" |
| Years on date columns | "orders placed in 2024", "customers who signed up after 2022" |
| Months, quarters and halves | "in March 2024", "the first quarter of 2024" (half-open ranges, so times on the last day still count) |
| Relative periods | "the last 30 days", "last month", "this year" (resolved against today's date, and the explanation states which dates were used) |
| Grouping by year / month / weekday | "orders per month", "the month with the most orders" |

### Behaviour

- **Read-only.** Modification requests are refused, and every query runs on a read-only connection.
- **Unanswerable requests are refused.** This covers predictions, opinions, and facts the database does not store.
- **Uncertain readings are labelled as uncertain**, and their alternatives are shown.
- **Empty results come with an explanation:** which condition removes every row, and what range of dates the data covers.

## Known limitations

These are measured or observed, not hypothetical:

- **Spider accuracy.** Execution accuracy is roughly 75–79% on development samples and lower on hard and extra-hard questions. See the README for current numbers. Remaining errors are mostly:
  - wrong output-column choices;
  - grouping keys;
  - Spider's own conventions, such as whether to show the ranking value, DISTINCT, and gold errors.
- **Not supported:**
  - arithmetic across aggregation levels ("budget left after paying salaries" = budget − SUM(salary));
  - expressions with constants ("years worked by the end of 2024");
  - arbitrary nesting of AND/OR beyond IN-lists and grouped name pairs;
  - UNION of two differently filtered queries (except "either of two people");
  - recursive hierarchies (all reports under a manager, at any depth).
- **Derived measures** are proposed only from cue words and column names, for example revenue, value, spending, duration, difference, combined. A metric with an unusual name ("GMV") is not recognized.
- **Relative dates** use the computer's current date. On historical data they correctly return nothing, and the explanation says so.
- **Self-references** are inferred only for conventional names (`manager_id`, `supervisor_id`, `parent_id`, `reports_to` …) or declared foreign keys.
- **Large schemas.** Beyond about 90 columns, only the tables most relevant to the request are offered, so a question about an unmentioned table can be missed.
- **Jev is not fully deterministic.** The same question can occasionally get a different reading; verification and the uncertainty label reduce this but do not remove it.
- **Dialects.** Only SQLite. The compiler emits SQLite functions (`STRFTIME`, `JULIANDAY`) and SQLite's handling of types.
