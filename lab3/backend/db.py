"""SQLite persistence and explicit transaction boundaries."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('customer','merchant','rider','admin')),
    name TEXT NOT NULL,
    approval TEXT NOT NULL DEFAULT 'pending',
    enabled INTEGER NOT NULL DEFAULT 1,
    online INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY,
    merchant_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_open INTEGER NOT NULL DEFAULT 1,
    region TEXT NOT NULL DEFAULT '仙林校区',
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS dishes (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price INTEGER NOT NULL CHECK(price > 0),
    stock INTEGER NOT NULL CHECK(stock >= 0),
    on_sale INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS addresses (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    region TEXT NOT NULL,
    detail TEXT NOT NULL,
    contact TEXT NOT NULL,
    phone TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cart (
    customer_id INTEGER NOT NULL REFERENCES users(id),
    dish_id INTEGER NOT NULL REFERENCES dishes(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    seen_price INTEGER NOT NULL,
    PRIMARY KEY(customer_id, dish_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    status TEXT NOT NULL,
    food_amount INTEGER NOT NULL,
    delivery_amount INTEGER NOT NULL DEFAULT 500,
    address_snapshot TEXT NOT NULL,
    created_at REAL NOT NULL,
    paid_at REAL,
    ready_at REAL,
    delivered_at REAL,
    completed_at REAL,
    cancelled_at REAL,
    reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS orders_customer_time ON orders(customer_id, created_at);
CREATE INDEX IF NOT EXISTS orders_shop_status ON orders(shop_id, status);
CREATE INDEX IF NOT EXISTS orders_status_time ON orders(status, created_at);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    dish_id INTEGER NOT NULL REFERENCES dishes(id),
    name TEXT NOT NULL,
    unit_price INTEGER NOT NULL,
    quantity INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS items_order ON order_items(order_id);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    amount INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    food_amount INTEGER NOT NULL CHECK(food_amount >= 0),
    delivery_amount INTEGER NOT NULL CHECK(delivery_amount >= 0),
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    rider_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL,
    ready_at REAL NOT NULL,
    offered_at REAL,
    completed_at REAL,
    escalated INTEGER NOT NULL DEFAULT 0,
    dispatch_after REAL NOT NULL DEFAULT 0,
    position_tick INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tasks_rider_status ON tasks(rider_id, status);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    rider_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    reporter_id INTEGER REFERENCES users(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resolution TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_exception
ON exceptions(order_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    result TEXT NOT NULL,
    changes TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_time ON audit_logs(created_at);
CREATE TABLE IF NOT EXISTS requests (
    actor_id INTEGER NOT NULL REFERENCES users(id),
    request_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(actor_id, request_key)
);
CREATE TABLE IF NOT EXISTS sms_codes (
    phone TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    sent_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path):
    """Open one independent connection; never share it across threads."""
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def initialize(path):
    """Create schema without resetting existing user data."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()


@contextmanager
def transaction(path, write=False):
    """Serialize writers so stock, capacity, and money change atomically."""
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
