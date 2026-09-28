"""SQLite (WAL mode) storage for songs and jobs."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY,
    video_id    TEXT NOT NULL,
    format      TEXT NOT NULL,
    ext         TEXT NOT NULL,
    filename    TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    uploader    TEXT,
    duration    REAL,
    size        INTEGER NOT NULL DEFAULT 0,
    source_url  TEXT NOT NULL,
    parent_id   INTEGER,
    edit_n      INTEGER,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS songs_video ON songs (video_id, format);
CREATE INDEX IF NOT EXISTS songs_expires ON songs (expires_at);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,          -- download | edit | preview
    status          TEXT NOT NULL,          -- queued | running | done | partial | failed
    url             TEXT,
    format          TEXT,
    lifetime        TEXT,
    single          INTEGER NOT NULL DEFAULT 0,
    title           TEXT,
    params          TEXT,                   -- JSON (edit/preview options)
    song_id         INTEGER,                -- source song for edit/preview
    result_song_id  INTEGER,
    preview_file    TEXT,
    error           TEXT,
    client_ip       TEXT,
    estimate_bytes  INTEGER NOT NULL DEFAULT 0,   -- edit jobs: expected output size
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    heartbeat_at    INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, kind);

CREATE TABLE IF NOT EXISTS tracks (
    id        INTEGER PRIMARY KEY,
    job_id    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    video_id  TEXT NOT NULL,
    title     TEXT,
    uploader  TEXT,
    duration  REAL,
    status    TEXT NOT NULL,                -- pending | running | done | failed | skipped | duplicate
    estimate  INTEGER NOT NULL DEFAULT 0,   -- expected size in bytes (quota reservation)
    error     TEXT,
    song_id   INTEGER
);
CREATE INDEX IF NOT EXISTS tracks_job ON tracks (job_id, idx);
"""

_local = threading.local()


def now() -> int:
    return int(time.time())


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get(path: Path) -> sqlite3.Connection:
    """One connection per thread (autocommit; use `with tx(conn)` for transactions)."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(str(path))
    if conn is None:
        conn = conns[str(path)] = connect(path)
    return conn


class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
