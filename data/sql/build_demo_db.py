"""Seed a small SQLite database for the sql-steward leg of the demo.

Idempotent: drops and recreates the two tables each run. The semantic layer
(semantic.yaml, next to this file) is the contract sql-steward exposes over it —
the agent never sees this schema directly, only what the layer permits.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "demo.db"

CUSTOMERS = [
    (1, "Ada Lovelace", "ada@example.com", "UK"),
    (2, "Alan Turing", "alan@example.com", "UK"),
    (3, "Grace Hopper", "grace@example.com", "US"),
    (4, "Katherine Johnson", "katherine@example.com", "US"),
    (5, "Edsger Dijkstra", "edsger@example.com", "NL"),
]

SUBSCRIPTIONS = [
    (1, 1, "pro", 49.0, "active"),
    (2, 2, "team", 199.0, "active"),
    (3, 3, "pro", 49.0, "active"),
    (4, 4, "enterprise", 999.0, "active"),
    (5, 5, "pro", 49.0, "cancelled"),
]


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS customers")
    cur.execute("DROP TABLE IF EXISTS subscriptions")
    cur.execute(
        "CREATE TABLE customers (id INTEGER, name TEXT, email TEXT, country TEXT)"
    )
    cur.execute(
        "CREATE TABLE subscriptions "
        "(id INTEGER, customer_id INTEGER, plan TEXT, mrr REAL, status TEXT)"
    )
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?)", CUSTOMERS)
    cur.executemany("INSERT INTO subscriptions VALUES (?,?,?,?,?)", SUBSCRIPTIONS)
    con.commit()
    con.close()
    print(f"seeded {DB} ({len(CUSTOMERS)} customers, {len(SUBSCRIPTIONS)} subscriptions)")


if __name__ == "__main__":
    main()
