"""SQLite storage. Datetimes are stored as naive ISO strings in clinic local time."""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_name  TEXT NOT NULL,
    phone         TEXT NOT NULL,
    start         TEXT NOT NULL,  -- 'YYYY-MM-DDTHH:MM', clinic local time
    end           TEXT NOT NULL,
    reason        TEXT,
    channel       TEXT NOT NULL CHECK (channel IN ('web', 'phone')),
    status        TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- A slot can hold only one active booking: guards against double booking
-- when the web page and a phone call race for the same slot.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_slot
    ON appointments (start) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments (phone);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
