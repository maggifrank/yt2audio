"""`yt-library cleanup`: recycle expired songs and repair drift between disk and database.

Safe to run while the app is running and safe to run repeatedly: every file
deletion happens inside the same SQLite write transaction as the row change,
and the worker moves files into the library inside its own write transaction,
so the two never interleave.
"""

from __future__ import annotations

import logging
import shutil
import time

from . import db, library
from .config import Config

log = logging.getLogger("yt_library.cleanup")

ORPHAN_GRACE = 10 * 60        # leave files younger than this alone
MISSING_GRACE = 10 * 60       # leave rows younger than this alone
FINISHED_JOB_KEEP = 7 * 86400  # forget finished jobs after a week


def run(cfg: Config) -> dict:
    db.init(cfg.db_path)
    conn = db.connect(cfg.db_path)
    stats = dict(expired=0, orphans=0, missing=0, partials=0, previews=0, stuck=0, jobs_pruned=0)
    try:
        now = db.now()

        # 1. expired songs: file and row
        with db.tx(conn):
            for r in conn.execute("SELECT * FROM songs WHERE expires_at <= ?", (now,)).fetchall():
                library.remove_quietly(library.song_path(cfg, r))
                conn.execute("DELETE FROM songs WHERE id = ?", (r["id"],))
                stats["expired"] += 1
                log.info("deleted expired song %d %r (%s)", r["id"], r["title"], r["filename"])

        # 2. rows whose file is missing
        with db.tx(conn):
            for r in conn.execute("SELECT * FROM songs WHERE created_at < ?", (now - MISSING_GRACE,)).fetchall():
                if not library.song_path(cfg, r).is_file():
                    conn.execute("DELETE FROM songs WHERE id = ?", (r["id"],))
                    stats["missing"] += 1
                    log.info("deleted song %d %r: file %s is missing", r["id"], r["title"], r["filename"])

        # 3. orphaned files with no row
        if cfg.library_dir.is_dir():
            with db.tx(conn):
                known = {r[0] for r in conn.execute("SELECT filename FROM songs")}
                for p in cfg.library_dir.iterdir():
                    if p.name.startswith(".") or not p.is_file() or p.name in known:
                        continue
                    if time.time() - p.stat().st_mtime < ORPHAN_GRACE:
                        continue
                    library.remove_quietly(p)
                    stats["orphans"] += 1
                    log.info("deleted orphaned file %s", p.name)

        # 4. stuck jobs (no heartbeat for YTL_JOB_TIMEOUT)
        with db.tx(conn):
            cut = now - cfg.job_timeout
            for j in conn.execute("SELECT id FROM jobs WHERE status = 'running' AND "
                                  "COALESCE(heartbeat_at, started_at, created_at) < ?", (cut,)).fetchall():
                conn.execute("UPDATE jobs SET status = 'failed', error = 'job got stuck and was stopped', "
                             "finished_at = ? WHERE id = ?", (now, j["id"]))
                conn.execute("UPDATE tracks SET status = 'failed', error = 'job got stuck' "
                             "WHERE job_id = ? AND status IN ('pending', 'running')", (j["id"],))
                stats["stuck"] += 1
                log.warning("marked stuck job %d as failed", j["id"])

        # 5. partial downloads: temp dirs of jobs that are no longer running
        tmp = cfg.tmp_dir
        if tmp.is_dir():
            with db.tx(conn):
                running = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE status = 'running'")}
                for p in tmp.iterdir():
                    parts = p.name.split("-")
                    job_id = int(parts[1]) if len(parts) > 2 and parts[0] == "job" and parts[1].isdigit() else None
                    if job_id in running:
                        continue
                    if time.time() - p.stat().st_mtime < ORPHAN_GRACE and job_id is None:
                        continue
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else library.remove_quietly(p)
                    stats["partials"] += 1
                    log.info("deleted partial download %s", p.name)

        # 6. stale previews (files and their jobs)
        with db.tx(conn):
            cut = now - cfg.preview_ttl
            for j in conn.execute("SELECT id, preview_file FROM jobs WHERE kind = 'preview' AND status NOT IN "
                                  "('queued', 'running') AND COALESCE(finished_at, created_at) < ?",
                                  (cut,)).fetchall():
                if j["preview_file"]:
                    library.remove_quietly(cfg.preview_dir / j["preview_file"])
                conn.execute("DELETE FROM jobs WHERE id = ?", (j["id"],))
                stats["previews"] += 1
            if cfg.preview_dir.is_dir():
                live = {r[0] for r in conn.execute("SELECT preview_file FROM jobs WHERE preview_file IS NOT NULL")}
                for p in cfg.preview_dir.iterdir():
                    if p.name not in live and time.time() - p.stat().st_mtime > cfg.preview_ttl:
                        library.remove_quietly(p)
                        stats["previews"] += 1
            if stats["previews"]:
                log.info("deleted %d stale previews", stats["previews"])

        # 7. forget old finished jobs
        with db.tx(conn):
            cur = conn.execute("DELETE FROM jobs WHERE status NOT IN ('queued', 'running') AND "
                               "COALESCE(finished_at, created_at) < ?", (now - FINISHED_JOB_KEEP,))
            stats["jobs_pruned"] = cur.rowcount

        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        conn.close()
    log.info("cleanup done: %s", ", ".join(f"{k}={v}" for k, v in stats.items()))
    return stats
