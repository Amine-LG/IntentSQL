"""Offline test doubles: a scripted Jev and a small relational fixture database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from intentsql.mock import MockClient

SHOP_DDL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, vip TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                     placed TEXT, status TEXT, total REAL);
CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id),
                          product_id INTEGER REFERENCES products(id), quantity INTEGER,
                          PRIMARY KEY (order_id, product_id));
CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary REAL,
                        manager_id INTEGER);
INSERT INTO customers VALUES (1,'Ann','Paris','Y'),(2,'Bob','Lyon','N'),(3,'Cy','Paris','N'),
                             (4,'Di','Nice','N'),(5,'Ed','Lyon','Y');
INSERT INTO products VALUES (1,'Latte','coffee',3.5),(2,'Mocha','coffee',4.0),
                            (3,'Green','tea',2.5),(4,'Scone','food',3.0);
INSERT INTO orders VALUES (1,1,'2024-01-05','paid',10),(2,1,'2024-02-10','paid',20),
                          (3,2,'2024-02-11','open',5),(4,3,'2024-03-01','paid',7.5),
                          (5,5,'2024-03-09','cancelled',12);
INSERT INTO order_items VALUES (1,1,2),(1,4,1),(2,2,3),(3,3,1),(4,1,1),(4,2,1),(5,4,4);
INSERT INTO employees VALUES (1,'Zoe','sales',5000,NULL),(2,'Yan','sales',4000,1),
                             (3,'Xia','tech',7000,NULL),(4,'Wim','tech',6500,3),(5,'Val','tech',3000,3);
"""


def make_shop(directory: str | Path) -> Path:
    path = Path(directory) / "shop.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(SHOP_DDL)
    return path


class ScriptedJev(MockClient):
    """The offline mock provider with scripted rules (see intentsql/mock.py)."""
