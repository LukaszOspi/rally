"""SQLite storage for the rally finder.

The dataset is tiny (~115 stations, ~70 routes, ~100 live windows), so a single
file with WAL enabled is plenty and keeps the deployment to two containers.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("RALLY_DB", "/data/rally.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    city              TEXT,
    country           TEXT,
    country_name      TEXT,
    address           TEXT,
    zip               TEXT,
    timezone          TEXT,
    google_link       TEXT,
    latitude          REAL,
    longitude         REAL,
    one_way           INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    key               TEXT PRIMARY KEY,
    start_station_id  INTEGER NOT NULL,
    end_station_id    INTEGER NOT NULL,
    start_date        TEXT NOT NULL,
    end_date          TEXT NOT NULL,
    window_days       INTEGER NOT NULL,
    distance_km       REAL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    gone_at           TEXT,
    detail_json       TEXT,
    detail_fetched_at TEXT,
    price             REAL,
    currency          TEXT,
    -- NULL = not checked yet, 1 = a vehicle is really bookable,
    -- 0 = the window is listed but booking refuses it or has nothing free.
    bookable          INTEGER,
    FOREIGN KEY (start_station_id) REFERENCES stations(id),
    FOREIGN KEY (end_station_id)   REFERENCES stations(id)
);

CREATE INDEX IF NOT EXISTS idx_offers_active     ON offers(active, start_date);
CREATE INDEX IF NOT EXISTS idx_offers_first_seen ON offers(first_seen);
CREATE INDEX IF NOT EXISTS idx_offers_start_stn  ON offers(start_station_id);
CREATE INDEX IF NOT EXISTS idx_offers_end_stn    ON offers(end_station_id);

CREATE TABLE IF NOT EXISTS watches (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    criteria_json     TEXT NOT NULL,
    chat_id           TEXT,
    linked_at         TEXT,
    created_at        TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    last_notified_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_watches_chat ON watches(chat_id);

CREATE TABLE IF NOT EXISTS watch_hits (
    watch_id          TEXT NOT NULL,
    offer_key         TEXT NOT NULL,
    notified_at       TEXT NOT NULL,
    PRIMARY KEY (watch_id, offer_key)
);

CREATE TABLE IF NOT EXISTS crawls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    ok                INTEGER,
    n_stations        INTEGER,
    n_routes          INTEGER,
    n_offers          INTEGER,
    n_new             INTEGER,
    n_gone            INTEGER,
    n_detail_fetched  INTEGER,
    duration_s        REAL,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key               TEXT PRIMARY KEY,
    value             TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """One connection per thread; the scheduler and request handlers differ."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
    _migrate()


def _migrate() -> None:
    """Additive column migrations for databases created by earlier versions."""
    conn = get_conn()
    have = {r["name"] for r in conn.execute("PRAGMA table_info(offers)")}
    for column, ddl in (("bookable", "INTEGER"),):
        if column not in have:
            with tx() as c:
                c.execute(f"ALTER TABLE offers ADD COLUMN {column} {ddl}")


def get_meta(key: str, default=None):
    row = get_conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def loads(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default
