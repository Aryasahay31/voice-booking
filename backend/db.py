"""SQLite storage for services and appointments."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "salon.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    duration_min INTEGER NOT NULL,
    price        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id    INTEGER NOT NULL REFERENCES services(id),
    customer_name TEXT NOT NULL,
    phone         TEXT NOT NULL,
    start_time    TEXT NOT NULL,   -- local time, 'YYYY-MM-DDTHH:MM'
    end_time      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'booked',  -- booked | cancelled
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS callback_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone         TEXT NOT NULL,
    reason        TEXT NOT NULL,
    urgency       TEXT NOT NULL DEFAULT 'normal',  -- normal | urgent
    status        TEXT NOT NULL DEFAULT 'open',    -- open | done
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_appt_time  ON appointments(start_time);
CREATE INDEX IF NOT EXISTS idx_appt_phone ON appointments(phone);
"""

SEED_SERVICES = [
    ("Haircut", 30, 500),
    ("Hair Colour", 90, 2500),
    ("Beard Trim", 15, 200),
    ("Facial", 60, 1200),
    ("Manicure", 45, 700),
]


@contextmanager
def connect():
    # isolation_level=None -> we control transactions explicitly (see `transaction`)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE takes the write lock up front, so two people can't
    grab the same slot at the same moment (check-then-insert is atomic)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO services (name, duration_min, price) VALUES (?, ?, ?)",
                SEED_SERVICES,
            )