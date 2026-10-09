"""Source of benchmarks/suites/adversarial.json (written by hand, validated by execution).

Categories follow the v1 brief: ambiguous column names, paraphrases of one
request, irrelevant tables, singular/plural and synonyms, implicit
relationships, unusual ordering language, second-highest, never/anti-joins,
per-group comparisons, nested aggregates, NULL semantics, combined
aggregation + filtering + joins, misleading wording and unsupported requests.
A list of gold queries means every listed reading is acceptable.
"""
DB = "retail.sqlite"

CASES = [
    # --- ambiguous column names: `name` exists in eight tables -----------------------------
    ("amb-1", "ambiguous_names", "List the names of all suppliers.", "SELECT name FROM suppliers"),
    ("amb-2", "ambiguous_names", "What are the names of the stores in Lyon?",
     "SELECT name FROM stores WHERE city = 'Lyon'"),
    ("amb-3", "ambiguous_names", "Which products are in the Tea category? Give their names.",
     "SELECT T1.name FROM products T1 JOIN categories T2 ON T1.category_id = T2.id WHERE T2.name = 'Tea'"),
    ("amb-4", "ambiguous_names", "Show each product name together with its category name.",
     "SELECT T1.name, T2.name FROM products T1 JOIN categories T2 ON T1.category_id = T2.id"),
    ("amb-5", "ambiguous_names", "Which department does Alice Martin work in?",
     "SELECT T2.name FROM employees T1 JOIN departments T2 ON T1.department_id = T2.id WHERE T1.name = 'Alice Martin'"),
    ("amb-6", "ambiguous_names", "List the names of the regions.", "SELECT name FROM regions"),

    # --- synonyms and singular/plural -----------------------------------------------------
    ("syn-1", "synonyms", "How many clients do we have?", "SELECT COUNT(*) FROM customers"),
    ("syn-2", "synonyms", "How many staff members work in the Engineering department?",
     "SELECT COUNT(*) FROM employees T1 JOIN departments T2 ON T1.department_id = T2.id WHERE T2.name = 'Engineering'"),
    ("syn-3", "synonyms", "Which vendor is based in Japan?", "SELECT name FROM suppliers WHERE country = 'Japan'"),
    ("syn-4", "synonyms", "What is the price of the croissant?", "SELECT price FROM products WHERE name = 'Croissant'"),
    ("syn-5", "synonyms", "list every category", "SELECT name FROM categories"),
    ("syn-6", "synonyms", "How many shops are there in each city?",
     "SELECT city, COUNT(*) FROM stores GROUP BY city"),

    # --- irrelevant tables (audit_log, app_settings must not be used) -----------------------
    ("irr-1", "irrelevant_tables", "How many orders were cancelled?",
     "SELECT COUNT(*) FROM orders WHERE status = 'cancelled'"),
    ("irr-2", "irrelevant_tables", "When was the Lille Central store opened?",
     "SELECT opened FROM stores WHERE name = 'Lille Central'"),

    # --- implicit relationships (multi-hop) -------------------------------------------------
    ("rel-1", "implicit_relationships", "Which customers bought Croissants? List their names.",
     "SELECT DISTINCT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id JOIN order_items T3 ON T3.order_id = T2.id JOIN products T4 ON T4.id = T3.product_id WHERE T4.name = 'Croissant'",
     ["SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id JOIN order_items T3 ON T3.order_id = T2.id JOIN products T4 ON T4.id = T3.product_id WHERE T4.name = 'Croissant'"]),
    ("rel-2", "implicit_relationships", "How many orders were placed in stores of the North region?",
     "SELECT COUNT(*) FROM orders T1 JOIN stores T2 ON T1.store_id = T2.id JOIN regions T3 ON T2.region_id = T3.id WHERE T3.name = 'North'"),
    ("rel-3", "implicit_relationships", "Which suppliers provide products in the Coffee category?",
     "SELECT DISTINCT T1.name FROM suppliers T1 JOIN products T2 ON T2.supplier_id = T1.id JOIN categories T3 ON T2.category_id = T3.id WHERE T3.name = 'Coffee'",
     ["SELECT T1.name FROM suppliers T1 JOIN products T2 ON T2.supplier_id = T1.id JOIN categories T3 ON T2.category_id = T3.id WHERE T3.name = 'Coffee'"]),

    # --- ordering language -------------------------------------------------------------------
    ("ord-1", "ordering", "List the tea products from cheapest to priciest.",
     "SELECT T1.name FROM products T1 JOIN categories T2 ON T1.category_id = T2.id WHERE T2.name = 'Tea' ORDER BY T1.price ASC", None, True),
    ("ord-2", "ordering", "Show employee names by seniority, longest-serving first.",
     "SELECT name FROM employees ORDER BY hire_date ASC", None, True),
    ("ord-3", "ordering", "Give the three most expensive products.",
     "SELECT name FROM products ORDER BY price DESC LIMIT 3", None, True),
    ("ord-4", "ordering", "What is the second highest salary?",
     "SELECT salary FROM employees ORDER BY salary DESC LIMIT 1 OFFSET 1",
     ["SELECT DISTINCT salary FROM employees ORDER BY salary DESC LIMIT 1 OFFSET 1"]),
    ("ord-5", "ordering", "Who earns the second highest salary?",
     "SELECT name FROM employees ORDER BY salary DESC LIMIT 1 OFFSET 1"),
    ("ord-6", "ordering", "Which category has the fewest products?",
     "SELECT T2.name FROM products T1 JOIN categories T2 ON T1.category_id = T2.id GROUP BY T2.id ORDER BY COUNT(*) ASC LIMIT 1"),

    # --- never / anti-joins (paraphrase group P1) -------------------------------------------
    ("never-1", "anti_join", "Which customers have never placed an order?",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P1"),
    ("never-2", "anti_join", "List the customers without any orders.",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P1"),
    ("never-3", "anti_join", "Show clients who did not order anything.",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P1"),
    ("never-4", "anti_join", "Names of customers with no orders at all?",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P1"),
    ("never-5", "anti_join", "I need the customers that never bought from us.",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P1"),
    ("never-6", "anti_join", "Find customers who haven't made a single purchase.",
     "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)", None, False, "P1"),
    ("never-7", "anti_join", "Which products have never been ordered?",
     "SELECT name FROM products WHERE id NOT IN (SELECT product_id FROM order_items)"),
    ("never-8", "anti_join", "Which customers never ordered any Espresso Beans?",
     "SELECT name FROM customers WHERE id NOT IN (SELECT T1.customer_id FROM orders T1 JOIN order_items T2 ON T2.order_id = T1.id JOIN products T3 ON T3.id = T2.product_id WHERE T3.name = 'Espresso Beans')"),
    ("never-9", "anti_join", "Which stores have no employees?",
     "SELECT name FROM stores WHERE id NOT IN (SELECT store_id FROM employees WHERE store_id IS NOT NULL)"),

    # --- per-group comparisons ----------------------------------------------------------------
    ("grp-1", "group_comparison", "Which employees earn more than the average salary of their department?",
     "SELECT T1.name FROM employees T1 WHERE T1.salary > (SELECT AVG(T2.salary) FROM employees T2 WHERE T2.department_id = T1.department_id)"),
    ("grp-2", "group_comparison", "Which products cost more than the average price?",
     "SELECT name FROM products WHERE price > (SELECT AVG(price) FROM products)"),
    ("grp-3", "group_comparison", "What is the most expensive product in each category?",
     "SELECT T1.name FROM products T1 WHERE T1.price = (SELECT MAX(T2.price) FROM products T2 WHERE T2.category_id = T1.category_id)",
     ["SELECT T2.name, T1.name FROM products T1 JOIN categories T2 ON T1.category_id = T2.id WHERE T1.price = (SELECT MAX(T3.price) FROM products T3 WHERE T3.category_id = T1.category_id)"]),
    ("grp-4", "group_comparison", "Which departments have an average salary above 5000?",
     "SELECT T2.name FROM employees T1 JOIN departments T2 ON T1.department_id = T2.id GROUP BY T2.id HAVING AVG(T1.salary) > 5000"),

    # --- nested aggregates and top-N groups (paraphrase group P2) ---------------------------
    ("nest-1", "nested_aggregate", "Which customers placed more orders than the average customer?",
     "SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id GROUP BY T1.id HAVING COUNT(*) > (SELECT COUNT(*) * 1.0 / (SELECT COUNT(*) FROM customers) FROM orders)",
     ["SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id GROUP BY T1.id HAVING COUNT(*) > (SELECT COUNT(*) * 1.0 / COUNT(DISTINCT customer_id) FROM orders)"]),
    ("topn-1", "aggregation_join", "What are the top 3 categories by average order line value?",
     "SELECT T3.name FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id JOIN categories T3 ON T2.category_id = T3.id GROUP BY T3.id ORDER BY AVG(T1.quantity * T1.unit_price) DESC LIMIT 3",
     ["SELECT T3.name, AVG(T1.quantity * T1.unit_price) FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
      "JOIN categories T3 ON T2.category_id = T3.id GROUP BY T3.id ORDER BY AVG(T1.quantity * T1.unit_price) DESC LIMIT 3"],
     True, "P2"),
    ("topn-2", "aggregation_join", "Which three categories have the highest average sales amount per order line?",
     "SELECT T3.name FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id JOIN categories T3 ON T2.category_id = T3.id GROUP BY T3.id ORDER BY AVG(T1.quantity * T1.unit_price) DESC LIMIT 3",
     ["SELECT T3.name, AVG(T1.quantity * T1.unit_price) FROM order_items T1 JOIN products T2 ON T1.product_id = T2.id "
      "JOIN categories T3 ON T2.category_id = T3.id GROUP BY T3.id ORDER BY AVG(T1.quantity * T1.unit_price) DESC LIMIT 3"],
     True, "P2"),

    # --- NULL semantics ---------------------------------------------------------------------
    ("null-1", "nulls", "Which customers have no email address?", "SELECT name FROM customers WHERE email IS NULL"),
    ("null-2", "nulls", "How many customers have an email on file?", "SELECT COUNT(*) FROM customers WHERE email IS NOT NULL",
     ["SELECT COUNT(email) FROM customers"]),
    ("null-3", "nulls", "Which employees have no manager?", "SELECT name FROM employees WHERE manager_id IS NULL"),

    # --- aggregation + filtering + joins -----------------------------------------------------
    ("agg-1", "aggregation_join", "How many shipped orders are there in each region? Most first.",
     "SELECT T3.name, COUNT(*) FROM orders T1 JOIN stores T2 ON T1.store_id = T2.id JOIN regions T3 ON T2.region_id = T3.id WHERE T1.status = 'shipped' GROUP BY T3.id ORDER BY COUNT(*) DESC", None, True),
    ("agg-2", "aggregation_join", "What is the average price of products supplied from France?",
     "SELECT AVG(T1.price) FROM products T1 JOIN suppliers T2 ON T1.supplier_id = T2.id WHERE T2.country = 'France'"),
    ("agg-3", "aggregation_join", "Which store has the most orders?",
     "SELECT T2.name FROM orders T1 JOIN stores T2 ON T1.store_id = T2.id GROUP BY T2.id ORDER BY COUNT(*) DESC LIMIT 1"),
    ("agg-4", "aggregation_join", "How many different products has each customer from Paris ordered?",
     "SELECT T1.name, COUNT(DISTINCT T3.product_id) FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id JOIN order_items T3 ON T3.order_id = T2.id WHERE T1.city = 'Paris' GROUP BY T1.id"),
    ("agg-5", "aggregation_join", "What is the total salary budget of the Sales department?",
     "SELECT SUM(T1.salary) FROM employees T1 JOIN departments T2 ON T1.department_id = T2.id WHERE T2.name = 'Sales'"),
    ("agg-6", "aggregation_join", "List gold-tier customers who placed at least 5 orders.",
     "SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id WHERE T1.loyalty_tier = 'gold' GROUP BY T1.id HAVING COUNT(*) >= 5"),
    ("agg-7", "aggregation_join", "What is the minimum, maximum and average price of coffee products?",
     "SELECT MIN(T1.price), MAX(T1.price), AVG(T1.price) FROM products T1 JOIN categories T2 ON T1.category_id = T2.id WHERE T2.name = 'Coffee'"),
    ("agg-8", "aggregation_join", "Which customers bought both Croissant and Baguette?",
     "SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id JOIN order_items T3 ON T3.order_id = T2.id JOIN products T4 ON T4.id = T3.product_id WHERE T4.name = 'Croissant' INTERSECT SELECT T1.name FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id JOIN order_items T3 ON T3.order_id = T2.id JOIN products T4 ON T4.id = T3.product_id WHERE T4.name = 'Baguette'"),
    ("agg-9", "aggregation_join", "How many orders did each customer place? Include their names.",
     "SELECT T1.name, COUNT(*) FROM customers T1 JOIN orders T2 ON T2.customer_id = T1.id GROUP BY T1.id"),

    # --- dates ----------------------------------------------------------------------------------
    ("date-1", "dates", "How many orders were placed after 2024-06-30?",
     "SELECT COUNT(*) FROM orders WHERE order_date > '2024-06-30'"),
    ("date-2", "dates", "Which customers signed up before March 1, 2022?",
     "SELECT name FROM customers WHERE signup_date < '2022-03-01'"),
    ("date-3", "dates", "How many orders were placed between 2024-03-01 and 2024-03-31?",
     "SELECT COUNT(*) FROM orders WHERE order_date BETWEEN '2024-03-01' AND '2024-03-31'"),

    # --- misleading wording ---------------------------------------------------------------------
    ("mis-1", "misleading", "Show me the products whose names contain the word Tea.",
     "SELECT name FROM products WHERE name LIKE '%Tea%'"),
    ("mis-2", "misleading", "How many products are discontinued?", "SELECT COUNT(*) FROM products WHERE discontinued = 1"),
    ("mis-3", "misleading", "Which orders were not shipped? Give their ids.", "SELECT id FROM orders WHERE status != 'shipped'"),
    ("mis-4", "misleading", "Is there any supplier from Brazil? List them.",
     "SELECT name FROM suppliers WHERE country = 'Brazil'"),

    # --- self-reference ---------------------------------------------------------------------------
    ("self-1", "self_reference", "How many employees report to each manager? Show the manager's name.",
     "SELECT T2.name, COUNT(*) FROM employees T1 JOIN employees T2 ON T1.manager_id = T2.id GROUP BY T2.id"),

    # --- unsupported or out-of-scope: must be refused -------------------------------------------
    ("ref-1", "unsupported", "Delete all cancelled orders.", None),
    ("ref-2", "unsupported", "Raise every salary in Support by 10 percent.", None),
    ("ref-3", "unsupported", "What will our sales be next year?", None),
    ("ref-4", "unsupported", "Which customers are the happiest with our service?", None),
    ("ref-5", "unsupported", "Translate all product names into Spanish.", None),
    ("ref-6", "unsupported", "What is the weather in Paris today?", None),
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
    return {"suite": "adversarial",
            "description": "Hand-written adversarial requests over a deliberately ambiguous retail schema.",
            "cases": cases}
