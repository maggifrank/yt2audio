"""In-process job queue.

Worker threads inside the web process:
- the main lane runs downloads and edits. Up to YTL_PARALLEL_DOWNLOADS jobs run
  at once, and a playlist job downloads up to that many tracks at once; a shared
  limit keeps the total number of yt2audio/audiocrop processes at that number too.
  Jobs start in submission order.
- the preview lane runs 15-second editor previews one at a time, so a preview is
  never stuck behind long downloads.
Jobs live in SQLite, so the queue survives restarts: jobs interrupted by a
restart are re-queued once when the service starts again.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, library, ytdl
from .config import FORMAT_EXT, Config

log = logging.getLogger("yt_library")

DOWNLOAD_TIMEOUT = 45 * 60   # per track
AUTO_RETRY_DELAY = 20        # seconds before failed tracks of a job are retried once
EDIT_TIMEOUT = 30 * 60


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = threading.Event()
        self.wake = {"main": threading.Event(), "preview": threading.Event()}
        self.threads: list[threading.Thread] = []
        # at most N yt2audio/audiocrop processes in the main lane, across all jobs
        self.slots = threading.BoundedSemaphore(cfg.parallel_downloads)
        # one download per (video, format) at a time, so parallel jobs never fetch the same song twice
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._key_guard = threading.Lock()

    # ---- lifecycle ----
    def start(self) -> None:
        self.recover()
        lanes = ["main"] * self.cfg.parallel_downloads + ["preview"]
        for i, lane in enumerate(lanes):
            t = threading.Thread(target=self._loop, args=(lane,), name=f"worker-{lane}-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self) -> None:
        self.stop.set()
        for ev in self.wake.values():
            ev.set()
        ytdl.kill_all()
        for t in self.threads:
            t.join(timeout=10)

    def notify(self, kind: str) -> None:
        self.wake["preview" if kind == "preview" else "main"].set()

    def recover(self) -> None:
        """Jobs left 'running' by a previous process: re-queue once, else fail."""
        conn = db.get(self.cfg.db_path)
        with db.tx(conn):
            for j in conn.execute("SELECT id, kind, attempts FROM jobs WHERE status = 'running'").fetchall():
                if j["kind"] != "preview" and j["attempts"] < 2:
                    conn.execute("UPDATE jobs SET status = 'queued', started_at = NULL WHERE id = ?", (j["id"],))
                    conn.execute("UPDATE tracks SET status = 'pending' WHERE job_id = ? AND status = 'running'",
                                 (j["id"],))
                    log.info("job %s re-queued after restart", j["id"])
                else:
                    self._finish(conn, j["id"], "failed", "interrupted by a service restart")

    # ---- loop ----
    def _loop(self, lane: str) -> None:
        kinds = ("preview",) if lane == "preview" else ("download", "edit")
        while not self.stop.is_set():
            job = None
            try:
                job = self._claim(kinds)
            except Exception:
                log.exception("claiming a job failed")
            if job is None:
                self.wake[lane].wait(timeout=3)
                self.wake[lane].clear()
                continue
            try:
                getattr(self, f"_run_{job['kind']}")(job)
            except Exception as e:  # a bug must not kill the worker thread
                log.exception("job %s crashed", job["id"])
                conn = db.get(self.cfg.db_path)
                with db.tx(conn):
                    self._finish(conn, job["id"], "failed", f"internal error: {e}")

    def _claim(self, kinds: tuple[str, ...]):
        conn = db.get(self.cfg.db_path)
        q = ",".join("?" * len(kinds))
        with db.tx(conn):
            row = conn.execute(f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN ({q}) ORDER BY id LIMIT 1",
                               kinds).fetchone()
            if row is None:
                return None
            now = db.now()
            conn.execute("UPDATE jobs SET status = 'running', started_at = ?, heartbeat_at = ?, "
                         "attempts = attempts + 1 WHERE id = ?", (now, now, row["id"]))
        return row

    def _finish(self, conn, job_id: int, status: str, error: str | None = None, **extra) -> None:
        sets = ", ".join(f"{k} = ?" for k in extra)
        conn.execute(f"UPDATE jobs SET status = ?, error = ?, finished_at = ?{', ' + sets if sets else ''} "
                     "WHERE id = ?", (status, error, db.now(), *extra.values(), job_id))
        conn.execute("UPDATE tracks SET status = 'failed', error = COALESCE(error, ?) "
                     "WHERE job_id = ? AND status IN ('pending', 'running')", (error or "cancelled", job_id))

    def _still_running(self, conn, job_id: int) -> bool:
        r = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return r is not None and r["status"] == "running"

    def _job_tmpdir(self, job_id: int) -> Path:
        self.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"job-{job_id}-", dir=self.cfg.tmp_dir))

    # ---- download ----
    def _run_download(self, job) -> None:
        cfg, conn = self.cfg, db.get(self.cfg.db_path)
        fmt, job_id = job["format"], job["id"]
        lifetime = cfg.lifetime(job["lifetime"]).seconds if job["lifetime"] in {l.key for l in cfg.lifetimes} \
            else cfg.lifetime(cfg.default_lifetime).seconds
        tmp = self._job_tmpdir(job_id)
        try:
            pending = conn.execute("SELECT * FROM tracks WHERE job_id = ? AND status = 'pending' ORDER BY idx",
                                   (job_id,)).fetchall()

            def one(t):
                tconn = db.get(cfg.db_path)
                if self.stop.is_set() or not self._still_running(tconn, job_id):
                    return
                try:
                    self._download_track(tconn, job_id, t, fmt, lifetime, tmp)
                except Exception as e:  # one broken track must not stop the others
                    log.exception("job %s: track %s crashed", job_id, t["video_id"])
                    self._set_track(tconn, t["id"], "failed", f"internal error: {e}")

            with ThreadPoolExecutor(max_workers=cfg.parallel_downloads,
                                    thread_name_prefix=f"job-{job_id}") as pool:
                list(pool.map(one, pending))
                # retry transient failures (e.g. YouTube's intermittent HTTP 403) once, after a pause
                again = [t for t in conn.execute(
                    "SELECT * FROM tracks WHERE job_id = ? AND status = 'failed' ORDER BY idx", (job_id,))
                    if library.retryable(t["error"])]
                if again and not self.stop.wait(AUTO_RETRY_DELAY) and self._still_running(conn, job_id):
                    log.info("job %s: retrying %d failed tracks", job_id, len(again))
                    conn.executemany("UPDATE tracks SET status = 'pending', error = NULL WHERE id = ?",
                                     [(t["id"],) for t in again])
                    list(pool.map(one, again))
            if self.stop.is_set() or not self._still_running(conn, job_id):
                return
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tracks WHERE job_id = ? GROUP BY status", (job_id,))}
            ok = counts.get("done", 0) + counts.get("duplicate", 0)
            bad = counts.get("failed", 0) + counts.get("skipped", 0)
            status = "done" if bad == 0 else ("partial" if ok else "failed")
            err = None if status == "done" else f"{bad} of {ok + bad} tracks could not be added"
            with db.tx(conn):
                if self._still_running(conn, job_id):
                    self._finish(conn, job_id, status, err)
            log.info("job %s %s: %s added, %s failed/skipped", job_id, status, ok, bad)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _set_track(self, conn, track_id: int, status: str, error: str | None = None, song_id=None) -> None:
        conn.execute("UPDATE tracks SET status = ?, error = ?, song_id = ? WHERE id = ?",
                     (status, error, song_id, track_id))

    def _key_lock(self, video_id: str, fmt: str) -> threading.Lock:
        with self._key_guard:
            return self._key_locks.setdefault((video_id, fmt), threading.Lock())

    def _download_track(self, conn, job_id: int, t, fmt: str, lifetime: int, tmp: Path) -> None:
        with self._key_lock(t["video_id"], fmt):
            self._download_track_locked(conn, job_id, t, fmt, lifetime, tmp)

    def _download_track_locked(self, conn, job_id: int, t, fmt: str, lifetime: int, tmp: Path) -> None:
        cfg = self.cfg
        now = db.now()
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (now, job_id))
        dup = library.find_duplicate(conn, t["video_id"], fmt)
        if dup is not None:
            with db.tx(conn):
                conn.execute("UPDATE songs SET expires_at = MAX(expires_at, ?) WHERE id = ?",
                             (now + min(lifetime, cfg.max_lifetime), dup["id"]))
                self._set_track(conn, t["id"], "duplicate", None, dup["id"])
            return
        try:
            with db.tx(conn):
                # the reservation for this track is already counted, so check with no extra
                library.check_quota(cfg, conn, 0)
        except library.QuotaExceeded:
            self._set_track(conn, t["id"], "failed", "storage quota is full")
            return
        self._set_track(conn, t["id"], "running")

        track_dir = tmp / f"{t['idx']}-{t['video_id']}"
        shutil.rmtree(track_dir, ignore_errors=True)
        track_dir.mkdir()
        with self.slots:
            if self.stop.is_set():
                return
            files, res = ytdl.download(cfg, library.source_url_for(t["video_id"]), fmt, track_dir,
                                       DOWNLOAD_TIMEOUT)
        # success is decided by the printed file path, not by yt2audio's exit code
        want_ext = FORMAT_EXT[fmt]
        files = [f for f in files if f.suffix.lower() == f".{want_ext}"]
        if not files:
            msg = "download timed out" if res.timed_out else ytdl.last_error(res.stderr, "download failed")
            self._set_track(conn, t["id"], "failed", msg)
            log.warning("job %s: %s failed: %s", job_id, t["video_id"], msg)
            shutil.rmtree(track_dir, ignore_errors=True)
            return
        src = files[0]
        meta = ytdl.ffprobe(src)
        duration = meta.get("duration") or t["duration"]
        if duration and duration > cfg.max_track_duration:
            self._set_track(conn, t["id"], "skipped",
                            f"longer than the {cfg.max_track_duration // 60} minute limit")
            shutil.rmtree(track_dir, ignore_errors=True)
            return
        filename = library.filename_for(fmt, t["video_id"])
        dest = cfg.library_dir / filename
        size = src.stat().st_size
        now = db.now()
        # move the file and insert its row inside one write transaction, so cleanup
        # (which also deletes files inside its transaction) never sees a half-added song
        with db.tx(conn):
            dup = library.find_duplicate(conn, t["video_id"], fmt)
            if dup is not None:  # someone else added it meanwhile (can't happen with one lane, but be safe)
                self._set_track(conn, t["id"], "duplicate", None, dup["id"])
                return
            conn.execute("DELETE FROM songs WHERE filename = ?", (filename,))  # expired row not yet cleaned
            os.replace(src, dest)
            os.utime(dest)  # yt-dlp may set the upload date as mtime; cleanup's grace period uses mtime
            cur = conn.execute(
                "INSERT INTO songs (video_id, format, ext, filename, title, uploader, duration, size, source_url, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t["video_id"], fmt, FORMAT_EXT[fmt], filename, t["title"] or meta.get("title") or t["video_id"],
                 t["uploader"] or meta.get("artist"), duration, size, library.source_url_for(t["video_id"]),
                 now, now + min(lifetime, cfg.max_lifetime)))
            self._set_track(conn, t["id"], "done", None, cur.lastrowid)
        shutil.rmtree(track_dir, ignore_errors=True)
        log.info("job %s: added %s as %s (%d bytes)", job_id, t["video_id"], filename, size)

    # ---- edit / preview ----
    def _source(self, conn, job):
        song = library.get_song(conn, job["song_id"])
        if song is None:
            return None, None
        path = library.song_path(self.cfg, song)
        return (song, path) if path.is_file() else (None, None)

    def _run_edit(self, job) -> None:
        cfg, conn = self.cfg, db.get(self.cfg.db_path)
        song, src = self._source(conn, job)
        if song is None:
            with db.tx(conn):
                self._finish(conn, job["id"], "failed", "the song no longer exists")
            return
        params = json.loads(job["params"])
        tmp = self._job_tmpdir(job["id"])
        try:
            ringtone = bool(params.get("ringtone"))
            ext, fmt = ("m4r", "m4r") if ringtone else (song["ext"], song["format"])
            out = tmp / f"edit.{ext}"
            with self.slots:
                res = ytdl.audiocrop(cfg, src, out, params, None, EDIT_TIMEOUT)
            if res.returncode != 0 or not out.is_file():
                with db.tx(conn):
                    self._finish(conn, job["id"], "failed", _crop_error(res))
                return
            meta = ytdl.ffprobe(out)
            with db.tx(conn):
                if not self._still_running(conn, job["id"]):
                    return
                n = conn.execute("SELECT COALESCE(MAX(edit_n), 0) + 1 FROM songs WHERE video_id = ?",
                                 (song["video_id"],)).fetchone()[0]
                while (cfg.library_dir / f"{song['video_id']}-edit-{n}.{ext}").exists():
                    n += 1
                filename = f"{song['video_id']}-edit-{n}.{ext}"
                dest = cfg.library_dir / filename
                os.replace(out, dest)
                os.utime(dest)
                cur = conn.execute(
                    "INSERT INTO songs (video_id, format, ext, filename, title, uploader, duration, size, source_url, "
                    "parent_id, edit_n, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (song["video_id"], fmt, ext, filename,
                     f"{song['title']} ({'ringtone' if ringtone else 'edit'})",
                     song["uploader"], meta.get("duration"), dest.stat().st_size, song["source_url"],
                     song["id"], n, db.now(), song["expires_at"]))
                self._finish(conn, job["id"], "done", None, result_song_id=cur.lastrowid)
            log.info("job %s: edit of song %s saved as %s", job["id"], song["id"], filename)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run_preview(self, job) -> None:
        cfg, conn = self.cfg, db.get(self.cfg.db_path)
        song, src = self._source(conn, job)
        if song is None:
            with db.tx(conn):
                self._finish(conn, job["id"], "failed", "the song no longer exists")
            return
        cfg.preview_dir.mkdir(parents=True, exist_ok=True)
        params = json.loads(job["params"])
        out = cfg.preview_dir / f"{job['id']}.{'m4r' if params.get('ringtone') else song['ext']}"
        from .config import PREVIEW_SECONDS
        res = ytdl.audiocrop(cfg, src, out, params, PREVIEW_SECONDS, EDIT_TIMEOUT)
        with db.tx(conn):
            if res.returncode != 0 or not out.is_file():
                library.remove_quietly(out)
                self._finish(conn, job["id"], "failed", _crop_error(res))
            else:
                self._finish(conn, job["id"], "done", None, preview_file=out.name)


def _crop_error(res: ytdl.Result) -> str:
    if res.timed_out:
        return "processing timed out"
    msg = ytdl.last_error(res.stderr, "audiocrop failed")
    if res.returncode == 3:
        return f"ffmpeg failed: {msg}" if msg != "ffmpeg failed" else "ffmpeg failed"
    return msg
