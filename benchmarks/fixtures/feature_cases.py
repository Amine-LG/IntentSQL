"""Source of benchmarks/suites/features.json: one or more requests per SQL capability.

Development suite (it is used to build and debug features).  Every capability
in docs/CAPABILITIES.md has cases here; several cases are paraphrase groups.
Golds are validated by execution (non-empty) by build_suites.py.  A list of
golds means every listed reading is acceptable, e.g. with or without the
ranking value shown next to the ranked names.
"""
DB = "retail.sqlite"

REVENUE = "T1.quantity * T1.unit_price"

CASES = [
    # --- basic ------------------------------------------------------------------------------------
    ("f-proj-1", "projection", "List every store with its city.", "SELECT name, city FROM stores"),
    ("f-cmp-1", "comparison", "Which products cost more than 100?", "SELECT name FROM products WHERE price > 100"),
    ("f-cmp-2", "comparison", "Which products cost at most 2.5?", "SELECT name FROM products WHERE price <= 2.5"),
    ("f-bool-1", "boolean", "Which gold-tier customers live in Paris or Lyon?",
     "SELECT name FROM customers WHERE city IN ('Paris', 'Lyon') AND loyalty_tier = 'gold'"),
    ("f-bool-2", "boolean", "List products that are discontinued or cost more than 100.",
     "SELECT name FROM products WHERE discontinued = 1 OR price > 100"),
    ("f-in-1", "in_list", "How many orders are pending or returned?",
     "SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'returned')"),
    ("f-between-1", "between", "Which products are priced between 3 and 4?",
     "SELECT name FROM products WHERE price BETWEEN 3 AND 4"),
    ("f-like-1", "text_match", "Which products have 'Blend' in their name?",
     "SELECT name FROM products WHERE name LIKE '%Blend%'"),
    ("f-like-2", "text_match", "Which suppliers' names start with K?",
     "SELECT name FROM suppliers WHERE name LIKE 'K%'"),
    ("f-null-1", "nulls", "How many customers have no email address?",
     "SELECT COUNT(*) FROM customers WHERE email IS NULL"),
    ("f-distinct-1", "distinct", "Which different cities do our customers live in?",
     "SELECT DISTINCT city FROM customers"),
    ("f-order-1", "ordering", "List the departments from the largest budget to the smallest.",
     "SELECT name FROM departments ORDER BY budget DESC", ["SELECT name, budget FROM departments ORDER BY budget DESC"],
     True),
    ("f-limit-1", "ordering", "What are the three most expensive products?",
     "SELECT name FROM products ORDER BY price DESC LIMIT 3",
     ["SELECT name, price FROM products ORDER BY price DESC LIMIT 3"], True),
    ("f-offset-1", "ordering", "Which product has the second highest price?",
     "SELECT name FROM products ORDER BY price DESC LIMIT 1 OFFSET 1",
     ["SELECT name, price FROM products ORDER BY price DESC LIMIT 1 OFFSET 1"]),

    # --- relationships -------------------------------------------------------------------------
    ("f-join-1", "join", "List each product with the country of its supplier.",
     "SELECT T1.name, T2.country FROM products T1 JOIN suppliers T2 ON T1.supplier_id = T2.id"),
    ("f-join-2", "multi_join", "Which regions have stores that sold Croissants?",
     "SELECT DISTINCT T5.name FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
     "JOIN orders T3 ON T1.order_id = T3.id JOIN stores T4 ON T3.store_id = T4.id "
     "JOIN regions T5 ON T4.region_id = T5.id WHERE T2.name = 'Croissant'"),
    ("f-self-1", "self_join", "Which employees work in the same store as their manager?",
     "SELECT T1.name FROM employees T1 JOIN employees T2 ON T1.manager_id = T2.id WHERE T1.store_id = T2.store_id"),
    ("f-self-2", "self_join", "List each employee together with the name of their manager.",
     "SELECT T1.name, T2.name FROM employees T1 JOIN employees T2 ON T1.manager_id = T2.id"),
    ("f-self-3", "self_join", "Which employees were hired before their manager?",
     "SELECT T1.name FROM employees T1 JOIN employees T2 ON T1.manager_id = T2.id WHERE T1.hire_date < T2.hire_date"),
    ("f-left-1", "outer_join", "How many orders does each customer have, including customers with no orders?",
     "SELECT T1.name, COUNT(T2.id) FROM customers T1 LEFT JOIN orders T2 ON T2.customer_id = T1.id GROUP BY T1.id"),

    # --- aggregation -----------------------------------------------------------------------------
    ("f-agg-1", "aggregate", "What is the average salary in each department?",
     "SELECT T2.name, AVG(T1.salary) FROM employees T1 JOIN departments T2 ON T1.department_id = T2.id GROUP BY T2.id"),
    ("f-having-1", "having", "Which departments have an average salary above 5000?",
     "SELECT T2.name FROM employees T1 JOIN departments T2 ON T1.department_id = T2.id GROUP BY T2.id HAVING AVG(T1.salary) > 5000"),
    ("f-cntd-1", "count_distinct", "How many different customers placed an order in March 2024?",
     "SELECT COUNT(DISTINCT customer_id) FROM orders WHERE order_date LIKE '2024-03%'"),
    ("f-nest-1", "nested_aggregate", "What is the average number of orders per customer?",
     "SELECT COUNT(*) * 1.0 / (SELECT COUNT(*) FROM customers) FROM orders",
     ["SELECT AVG(n) FROM (SELECT COUNT(*) AS n FROM orders GROUP BY customer_id)"]),
    ("f-nest-2", "nested_aggregate", "What is the average order value?",
     f"SELECT AVG(v) FROM (SELECT SUM({REVENUE}) AS v FROM order_items T1 GROUP BY T1.order_id)"),
    ("f-nest-3", "nested_aggregate", "Which customers placed more orders than the average customer?",
     "SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id GROUP BY T1.id "
     "HAVING COUNT(*) > (SELECT COUNT(*) * 1.0 / (SELECT COUNT(*) FROM customers) FROM orders)"),
    ("f-pct-1", "percentage", "What percentage of orders were cancelled?",
     "SELECT 100.0 * SUM(status = 'cancelled') / COUNT(*) FROM orders"),

    # --- expressions -------------------------------------------------------------------------------
    ("f-expr-1", "arithmetic", "What is the total revenue of each category?",
     f"SELECT T3.name, SUM({REVENUE}) FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
     "JOIN categories T3 ON T2.category_id = T3.id GROUP BY T3.id"),
    ("f-expr-2", "arithmetic", "Which product generated the most revenue?",
     f"SELECT T2.name FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id GROUP BY T2.id "
     f"ORDER BY SUM({REVENUE}) DESC LIMIT 1",
     [f"SELECT T2.name, SUM({REVENUE}) FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
      f"GROUP BY T2.id ORDER BY SUM({REVENUE}) DESC LIMIT 1"]),
    ("f-expr-3", "arithmetic", "Who are the top 3 customers by total spending?",
     f"SELECT T3.name FROM order_items T1 JOIN orders T2 ON T1.order_id = T2.id JOIN customers T3 "
     f"ON T2.customer_id = T3.id GROUP BY T3.id ORDER BY SUM({REVENUE}) DESC LIMIT 3",
     [f"SELECT T3.name, SUM({REVENUE}) FROM order_items T1 JOIN orders T2 ON T1.order_id = T2.id JOIN "
      f"customers T3 ON T2.customer_id = T3.id GROUP BY T3.id ORDER BY SUM({REVENUE}) DESC LIMIT 3"], True),
    ("f-expr-4", "arithmetic", "How much budget does each department have left after paying salaries?",
     "SELECT T2.name, T2.budget - SUM(T1.salary) FROM employees T1 JOIN departments T2 ON "
     "T1.department_id = T2.id GROUP BY T2.id"),

    # --- advanced -------------------------------------------------------------------------------------
    ("f-anti-1", "not_exists", "Which suppliers have no discontinued products?",
     "SELECT name FROM suppliers WHERE id NOT IN (SELECT supplier_id FROM products WHERE discontinued = 1)"),
    ("f-anti-2", "not_exists", "Which customers have never placed a cancelled order?",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders WHERE status = 'cancelled')"),
    ("f-semi-1", "in_subquery", "Which customers bought products from Japanese suppliers?",
     "SELECT name FROM customers WHERE id IN (SELECT T2.customer_id FROM order_items T1 JOIN orders T2 "
     "ON T1.order_id = T2.id JOIN products T3 ON T1.product_id = T3.id JOIN suppliers T4 "
     "ON T3.supplier_id = T4.id WHERE T4.country = 'Japan')"),
    ("f-scalar-1", "scalar_subquery", "Which products cost more than the average product?",
     "SELECT name FROM products WHERE price > (SELECT AVG(price) FROM products)"),
    ("f-corr-1", "correlated", "Which employees earn more than the average salary of their department?",
     "SELECT T1.name FROM employees T1 WHERE T1.salary > (SELECT AVG(T2.salary) FROM employees T2 "
     "WHERE T2.department_id = T1.department_id)"),
    ("f-set-1", "set_operation", "Which stores have both cancelled and returned orders?",
     "SELECT T2.name FROM orders T1 JOIN stores T2 ON T1.store_id = T2.id WHERE T1.status = 'cancelled' "
     "INTERSECT SELECT T2.name FROM orders T1 JOIN stores T2 ON T1.store_id = T2.id WHERE T1.status = 'returned'"),
    ("f-set-2", "set_operation", "Which supplier countries supply both coffee and tea products?",
     "SELECT T2.country FROM products T1 JOIN suppliers T2 ON T1.supplier_id = T2.id JOIN categories T3 "
     "ON T1.category_id = T3.id WHERE T3.name = 'Coffee' INTERSECT SELECT T2.country FROM products T1 "
     "JOIN suppliers T2 ON T1.supplier_id = T2.id JOIN categories T3 ON T1.category_id = T3.id "
     "WHERE T3.name = 'Tea'"),
    ("f-topg-1", "top_per_group", "What is the most expensive product in each category?",
     "SELECT T1.name FROM products T1 WHERE T1.price = (SELECT MAX(T2.price) FROM products T2 "
     "WHERE T2.category_id = T1.category_id)",
     ["SELECT T2.name, T1.name FROM products T1 JOIN categories T2 ON T1.category_id = T2.id WHERE T1.price = "
      "(SELECT MAX(T3.price) FROM products T3 WHERE T3.category_id = T1.category_id)"]),
    ("f-topn-1", "top_per_group", "List the two cheapest products in each category.",
     "SELECT name FROM (SELECT name, ROW_NUMBER() OVER (PARTITION BY category_id ORDER BY price) AS r "
     "FROM products) WHERE r <= 2"),
    ("f-always-1", "universal", "Which categories have only products priced under 20?",
     "SELECT name FROM categories WHERE id NOT IN (SELECT category_id FROM products WHERE price >= 20)"),

    # --- dates -----------------------------------------------------------------------------------------
    ("f-year-1", "date_part", "How many orders were placed in 2024?",
     "SELECT COUNT(*) FROM orders WHERE order_date LIKE '2024%'"),
    ("f-year-2", "date_part", "How many customers signed up after 2022?",
     "SELECT COUNT(*) FROM customers WHERE STRFTIME('%Y', signup_date) > '2022'"),
    ("f-month-1", "date_part", "How many orders were placed in each month?",
     "SELECT STRFTIME('%Y-%m', order_date), COUNT(*) FROM orders GROUP BY 1"),
    ("f-month-2", "date_part", "Which month had the most orders?",
     "SELECT STRFTIME('%Y-%m', order_date) FROM orders GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
     ["SELECT STRFTIME('%Y-%m', order_date), COUNT(*) FROM orders GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1"]),
    ("f-range-1", "date_range", "How many orders were placed in the first quarter of 2024?",
     "SELECT COUNT(*) FROM orders WHERE order_date BETWEEN '2024-01-01' AND '2024-03-31'"),
    ("f-dur-1", "date_arithmetic", "How many years had each employee worked by the end of 2024? Show their names.",
     "SELECT name, (JULIANDAY('2024-12-31') - JULIANDAY(hire_date)) / 365.25 FROM employees"),

    # --- paraphrase groups -------------------------------------------------------------------------
    ("f-para-1", "paraphrase", "How many products does each supplier provide?",
     "SELECT T2.name, COUNT(*) FROM products T1 JOIN suppliers T2 ON T1.supplier_id = T2.id GROUP BY T2.id",
     None, False, "P-supplier-count"),
    ("f-para-2", "paraphrase", "For every supplier, count its products.",
     "SELECT T2.name, COUNT(*) FROM products T1 JOIN suppliers T2 ON T1.supplier_id = T2.id GROUP BY T2.id",
     None, False, "P-supplier-count"),
    ("f-para-3", "paraphrase", "Number of products per vendor?",
     "SELECT T2.name, COUNT(*) FROM products T1 JOIN suppliers T2 ON T1.supplier_id = T2.id GROUP BY T2.id",
     None, False, "P-supplier-count"),
    ("f-para-4", "paraphrase", "Which customers have not ordered anything?",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P-no-orders"),
    ("f-para-5", "paraphrase", "List customers without any orders.",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P-no-orders"),
    ("f-para-6", "paraphrase", "Who are the customers that never placed an order?",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P-no-orders"),

    # --- semantic distinctions (similar words, different meanings) --------------------------------
    ("f-sem-1", "semantic", "Top 3 products by revenue",
     f"SELECT T2.name FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id GROUP BY T2.id "
     f"ORDER BY SUM({REVENUE}) DESC LIMIT 3",
     [f"SELECT T2.name, SUM({REVENUE}) FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
      f"GROUP BY T2.id ORDER BY SUM({REVENUE}) DESC LIMIT 3"], True),
    ("f-sem-2", "semantic", "Top 3 products by number of orders",
     "SELECT T2.name FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id GROUP BY T2.id "
     "ORDER BY COUNT(*) DESC LIMIT 3",
     ["SELECT T2.name, COUNT(*) FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
      "GROUP BY T2.id ORDER BY COUNT(*) DESC LIMIT 3"], True),
    ("f-sem-3", "semantic", "What is the average order line value?",
     f"SELECT AVG({REVENUE}) FROM order_items T1"),
]


def suite() -> dict:
    cases = []
    for entry in CASES:
        case_id, category, question, gold = entry[:4]
        extra = entry[4] if len(entry) > 4 else None
        ordered = entry[5] if len(entry) > 5 else False
        group = entry[6] if len(entry) > 6 else None
        golds = ([gold] if gold else []) + (extra or [])
        cases.append({"id": case_id, "database": DB, "question": question, "gold_sql": golds,
                      "ordered": bool(ordered), "expect": "refuse" if gold is None else "answer",
                      "category": category, "group": group})
    return {"suite": "features",
            "description": "One or more requests per supported SQL capability (development suite).",
            "cases": cases}
