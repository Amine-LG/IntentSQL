"""Build benchmarks/fixtures/retail.sqlite: a deterministic, deliberately tricky schema.

Ambiguities by design: `name` exists in eight tables, `employees.manager_id`
references `employees`, customer emails may be NULL, `discontinued` is a 0/1
flag, statuses use several vocabularies, and two tables (`audit_log`,
`app_settings`) are irrelevant to every question.

    python benchmarks/fixtures/build_retail.py
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

OUT = Path(__file__).with_name("retail.sqlite")

DDL = """
CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
                     region_id INTEGER REFERENCES regions(id), opened TEXT);
CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT NOT NULL, budget REAL);
CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT NOT NULL, title TEXT, salary REAL,
                        hire_date TEXT, department_id INTEGER REFERENCES departments(id),
                        manager_id INTEGER REFERENCES employees(id),
                        store_id INTEGER REFERENCES stores(id));
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, city TEXT,
                        signup_date TEXT, loyalty_tier TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                       category_id INTEGER REFERENCES categories(id),
                       supplier_id INTEGER REFERENCES suppliers(id), price REAL,
                       discontinued INTEGER NOT NULL DEFAULT 0);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                     store_id INTEGER REFERENCES stores(id), order_date TEXT, status TEXT);
CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id),
                          product_id INTEGER REFERENCES products(id), quantity INTEGER,
                          unit_price REAL, PRIMARY KEY (order_id, product_id));
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, event TEXT, created_at TEXT);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
"""

REGIONS = ["North", "South", "East", "West", "Central"]
CITIES = ["Paris", "Lyon", "Nice", "Lille", "Nantes", "Bordeaux", "Toulouse", "Rennes"]
DEPARTMENTS = [("Sales", 120000), ("Engineering", 300000), ("Support", 80000),
               ("Marketing", 150000), ("Finance", 90000), ("Logistics", 110000)]
TITLES = ["Associate", "Senior Associate", "Manager", "Director"]
FIRST = ["Alice", "Bruno", "Chloe", "David", "Emma", "Farid", "Gina", "Hugo", "Ines", "Jules",
         "Karim", "Lea", "Marc", "Nina", "Omar", "Paul", "Quinn", "Rosa", "Sami", "Tara"]
LAST = ["Martin", "Bernard", "Dubois", "Durand", "Moreau", "Laurent", "Simon", "Michel",
        "Lefebvre", "Leroy", "Roux", "Fournier"]
CATEGORIES = ["Coffee", "Tea", "Bakery", "Snacks", "Beverages", "Equipment"]
SUPPLIERS = [("Andes Beans", "Peru"), ("Kyoto Leaf", "Japan"), ("Boulangerie Nord", "France"),
             ("Crunch Co", "USA"), ("Fizz Ltd", "UK"), ("Grind Works", "Germany"),
             ("Assam Gardens", "India"), ("Mill & Oven", "France")]
PRODUCTS = {"Coffee": ["Espresso Beans", "House Blend", "Decaf Roast", "Single Origin Peru",
                       "Cold Brew Pack", "Mocha Blend", "Latte Mix"],
            "Tea": ["Green Sencha", "Earl Grey", "Chai Spice", "Mint Infusion", "Assam Breakfast",
                    "Jasmine Pearl"],
            "Bakery": ["Croissant", "Baguette", "Pain au Chocolat", "Brioche", "Sourdough Loaf",
                       "Almond Tart", "Fruit Scone"],
            "Snacks": ["Sea Salt Chips", "Trail Mix", "Dark Chocolate Bar", "Granola Bar",
                       "Salted Pretzels"],
            "Beverages": ["Sparkling Water", "Orange Juice", "Lemonade", "Iced Tea Bottle",
                          "Kombucha"],
            "Equipment": ["French Press", "Pour Over Kit", "Grinder", "Kettle", "Espresso Machine",
                          "Milk Frother"]}
STATUSES = ["shipped", "shipped", "shipped", "pending", "cancelled", "returned"]
TIERS = ["gold", "silver", "bronze", "bronze"]


def build(path: Path = OUT) -> Path:
    rng = random.Random(20261009)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.executemany("INSERT INTO regions VALUES (?, ?)", list(enumerate(REGIONS, 1)))
    stores = []
    for i in range(1, 13):
        city = CITIES[(i - 1) % len(CITIES)]
        stores.append((i, f"{city} {'Central' if i <= 8 else 'Station'}", city,
                       rng.randint(1, len(REGIONS)), f"20{rng.randint(10, 22):02d}-{rng.randint(1, 12):02d}-01"))
    conn.executemany("INSERT INTO stores VALUES (?, ?, ?, ?, ?)", stores)
    conn.executemany("INSERT INTO departments VALUES (?, ?, ?)",
                     [(i, n, b) for i, (n, b) in enumerate(DEPARTMENTS, 1)])
    employees = []
    used = set()
    for i in range(1, 41):
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used:
                used.add(name)
                break
        department = (i - 1) % len(DEPARTMENTS) + 1
        title = TITLES[3] if i <= 2 else TITLES[2] if i <= 8 else rng.choice(TITLES[:2])
        manager = None if i <= 2 else (rng.randint(1, 2) if i <= 8 else rng.randint(3, 8))
        salary = {"Director": 9000, "Manager": 6000}.get(title, 3000) + rng.randint(0, 40) * 50
        employees.append((i, name, title, float(salary),
                          f"20{rng.randint(12, 24):02d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                          department, manager, rng.randint(1, 12)))
    conn.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?, ?, ?)", employees)
    customers = []
    for i in range(1, 81):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        email = None if i % 7 == 0 else f"{name.lower().replace(' ', '.')}{i}@example.com"
        customers.append((i, name, email, rng.choice(CITIES),
                          f"202{rng.randint(1, 4)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                          rng.choice(TIERS)))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)
    conn.executemany("INSERT INTO categories VALUES (?, ?)", list(enumerate(CATEGORIES, 1)))
    conn.executemany("INSERT INTO suppliers VALUES (?, ?, ?)",
                     [(i, n, c) for i, (n, c) in enumerate(SUPPLIERS, 1)])
    products = []
    pid = 0
    for c, category in enumerate(CATEGORIES, 1):
        for name in PRODUCTS[category]:
            pid += 1
            base = {"Equipment": 40, "Coffee": 9, "Tea": 6}.get(category, 2)
            price = round(base + rng.random() * base * (4 if category == "Equipment" else 1.5), 2)
            products.append((pid, name, c, rng.randint(1, len(SUPPLIERS)), price,
                             1 if pid % 9 == 0 else 0))
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", products)
    orders, items = [], []
    active = [c[0] for c in customers if c[0] % 5 != 0]       # every fifth customer never orders
    for oid in range(1, 301):
        month = rng.randint(1, 12)
        orders.append((oid, rng.choice(active), rng.randint(1, 12),
                       f"2024-{month:02d}-{rng.randint(1, 28):02d}", rng.choice(STATUSES)))
        for product in rng.sample([p for p in products if not p[5]], rng.randint(1, 4)):
            items.append((oid, product[0], rng.randint(1, 5), product[4]))
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?)", items)
    conn.executemany("INSERT INTO audit_log VALUES (?, ?, ?)",
                     [(i, rng.choice(["login", "export", "price_change"]), f"2024-0{rng.randint(1, 9)}-01")
                      for i in range(1, 51)])
    conn.executemany("INSERT INTO app_settings VALUES (?, ?)", [("currency", "EUR"), ("theme", "dark")])
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    print(build())
