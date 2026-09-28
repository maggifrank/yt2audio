"""FastAPI app: JSON API and static frontend."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import config as C
from . import db, library, ytdl
from .security import RateLimiter, client_ip, origin_ok
from .util import BadURL, check_url, safe_filename, unique_names
from .worker import Worker
from .zipstream import stream_zip

log = logging.getLogger("yt_library")
STATIC = Path(__file__).parent / "static"
MAX_IDS = 5000


class ProbeBody(BaseModel):
    url: str = Field(max_length=500)


class JobBody(BaseModel):
    url: str = Field(max_length=500)
    format: str
    lifetime: str | None = None
    single: bool = False


class IdsBody(BaseModel):
    ids: list[int] = Field(max_length=MAX_IDS)


class RetryBody(BaseModel):
    tracks: list[int] | None = Field(default=None, max_length=MAX_IDS)


class ExtendBody(IdsBody):
    lifetime: str


class EditBody(BaseModel):
    start: float = 0
    end: float | None = None
    gain: float = 0
    normalize: bool = False
    target: float = C.TARGET_DEFAULT
    fade_in: float = 0
    fade_out: float = 0


def create_app(cfg: C.Config | None = None, start_worker: bool = True) -> FastAPI:
    cfg = cfg or C.load()
    worker = Worker(cfg)
    job_limiter = RateLimiter(cfg.rate_limit_count, cfg.rate_limit_window)
    # probes and previews are cheaper but still spawn processes: allow more of them
    probe_limiter = RateLimiter(cfg.rate_limit_count * 4, cfg.rate_limit_window)
    probe_cache: dict[str, tuple[float, dict]] = {}
    probe_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        cfg.library_dir.mkdir(parents=True, exist_ok=True)
        db.init(cfg.db_path)
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.shutdown()

    app = FastAPI(title="yt-library", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.worker = cfg, worker

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not origin_ok(cfg, request):
            return JSONResponse({"detail": "Cross-origin request rejected."}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    def conn():
        return db.get(cfg.db_path)

    def limit(limiter: RateLimiter, request: Request) -> str:
        ip = client_ip(cfg, request)
        wait = limiter.hit(ip)
        if wait:
            raise HTTPException(429, f"Too many requests from {ip}. Try again in {max(1, wait // 60)} min.",
                                headers={"Retry-After": str(wait)})
        return ip

    def song_or_404(song_id: int):
        row = library.get_song(conn(), song_id)
        if row is None:
            raise HTTPException(404, "Song not found (it may have expired or been deleted).")
        return row

    def do_probe(url: str) -> dict:
        now = time.time()
        with probe_lock:
            for k in [k for k, (t, _) in probe_cache.items() if now - t > 600]:
                del probe_cache[k]
            hit = probe_cache.get(url)
        if hit:
            return hit[1]
        try:
            info = ytdl.probe(cfg, url, cfg.max_tracks_per_job)
        except ytdl.ProbeError as e:
            raise HTTPException(400, f"Could not read this URL: {e}")
        with probe_lock:
            probe_cache[url] = (now, info)
        return info

    # ---- pages ----
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/edit", include_in_schema=False)
    def edit_page():
        return FileResponse(STATIC / "edit.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- config ----
    @app.get("/api/config")
    def get_config():
        return {
            "lifetimes": [{"key": l.key, "label": l.label, "seconds": l.seconds} for l in cfg.lifetimes],
            "default_lifetime": cfg.default_lifetime,
            "formats": C.FORMATS,
            "default_format": "mp3",
            "limits": {"max_tracks_per_job": cfg.max_tracks_per_job, "max_track_duration": cfg.max_track_duration,
                       "max_queued_jobs": cfg.max_queued_jobs, "quota_bytes": cfg.storage_quota},
            "edit": {"gain_min": C.GAIN_RANGE[0], "gain_max": C.GAIN_RANGE[1], "target_min": C.TARGET_RANGE[0],
                     "target_max": C.TARGET_RANGE[1], "target_default": C.TARGET_DEFAULT,
                     "fade_min": C.FADE_RANGE[0], "fade_max": C.FADE_RANGE[1],
                     "preview_seconds": C.PREVIEW_SECONDS},
            "server_time": db.now(),
        }

    # ---- probe / jobs ----
    def resolve(url: str, single: bool) -> tuple[dict, dict, list[dict]]:
        try:
            u = check_url(url)
        except BadURL as e:
            raise HTTPException(400, str(e))
        if single and u["video_id"]:
            info = do_probe(f"https://www.youtube.com/watch?v={u['video_id']}")
        else:
            info = do_probe(u["url"])
        return u, info, info["entries"]

    @app.post("/api/probe")
    async def probe(body: ProbeBody, request: Request):
        limit(probe_limiter, request)
        u, info, entries = await run_in_threadpool(resolve, body.url, False)
        existing = library.existing_by_video(conn(), [e["video_id"] for e in entries])
        vip = None
        if info["is_playlist"] and u["video_id"]:
            title = next((e["title"] for e in entries if e["video_id"] == u["video_id"]), None)
            vip = {"video_id": u["video_id"], "title": title}
        return {
            "url": u["url"], "title": info["title"], "is_playlist": info["is_playlist"],
            "video_in_playlist": vip, "track_count": len(entries),
            "too_many": len(entries) > cfg.max_tracks_per_job,
            "entries": [{**e, "too_long": bool(e["duration"] and e["duration"] > cfg.max_track_duration),
                         "existing": existing.get(e["video_id"], [])} for e in entries],
        }

    @app.post("/api/jobs", status_code=202)
    async def create_job(body: JobBody, request: Request):
        if body.format not in C.FORMATS:
            raise HTTPException(400, f"Unsupported format {body.format!r}.")
        lifetime = body.lifetime or cfg.default_lifetime
        try:
            cfg.lifetime(lifetime)
        except C.ConfigError:
            raise HTTPException(400, f"Lifetime must be one of: {', '.join(l.key for l in cfg.lifetimes)}.")
        try:
            check_url(body.url)
        except BadURL as e:
            raise HTTPException(400, str(e))
        if library.count_queued(conn()) >= cfg.max_queued_jobs:
            raise HTTPException(503, f"The queue is full ({cfg.max_queued_jobs} jobs). Try again later.")
        ip = limit(job_limiter, request)
        u, info, entries = await run_in_threadpool(resolve, body.url, body.single)
        if not entries:
            raise HTTPException(400, "No downloadable videos found at this URL.")
        if len(entries) > cfg.max_tracks_per_job:
            raise HTTPException(400, f"This playlist has more than {cfg.max_tracks_per_job} tracks, the "
                                     "per-job limit.")
        single = body.single or not info["is_playlist"]
        url = f"https://www.youtube.com/watch?v={u['video_id']}" if body.single and u["video_id"] else u["url"]
        try:
            job_id = library.create_download_job(cfg, conn(), url, info["title"], body.format, lifetime,
                                                 single, entries, ip)
        except library.QuotaExceeded as e:
            raise HTTPException(413, str(e))
        worker.notify("download")
        log.info("job %d queued by %s: %s (%d tracks, %s)", job_id, ip, url, len(entries), body.format)
        return job_json(job_id)

    def job_json(job_id: int) -> dict:
        row = conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found.")
        return library.job_dict(conn(), row)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: int, request: Request, body: RetryBody | None = None):
        if library.count_queued(conn()) >= cfg.max_queued_jobs:
            raise HTTPException(503, f"The queue is full ({cfg.max_queued_jobs} jobs). Try again later.")
        ip = limit(job_limiter, request)
        try:
            n = library.retry_job(cfg, conn(), job_id, body.tracks if body else None)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        except library.QuotaExceeded as e:
            raise HTTPException(413, str(e))
        worker.notify("download")
        log.info("job %d: %d failed tracks re-queued by %s", job_id, n, ip)
        return job_json(job_id)

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": library.list_jobs(conn()), "server_time": db.now()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        return job_json(job_id)

    # ---- songs ----
    @app.get("/api/songs")
    def list_songs():
        c = conn()
        return {"songs": library.list_songs(c), "used_bytes": library.used_bytes(c),
                "quota_bytes": cfg.storage_quota, "server_time": db.now()}

    @app.get("/api/songs/{song_id}")
    def get_song(song_id: int):
        return library.song_dict(song_or_404(song_id))

    @app.delete("/api/songs/{song_id}", status_code=204)
    def delete_song(song_id: int, request: Request):
        song_or_404(song_id)
        library.delete_songs(cfg, conn(), [song_id])
        log.info("song %d deleted by %s", song_id, client_ip(cfg, request))
        return Response(status_code=204)

    @app.post("/api/songs/delete")
    def delete_many(body: IdsBody, request: Request):
        n = library.delete_songs(cfg, conn(), body.ids)
        log.info("%d songs deleted by %s", n, client_ip(cfg, request))
        return {"deleted": n}

    @app.post("/api/songs/extend")
    def extend(body: ExtendBody):
        try:
            lt = cfg.lifetime(body.lifetime)
        except C.ConfigError:
            raise HTTPException(400, f"Lifetime must be one of: {', '.join(l.key for l in cfg.lifetimes)}.")
        library.extend_songs(cfg, conn(), body.ids, lt.seconds)
        ids = set(body.ids)
        return {"songs": [s for s in library.list_songs(conn()) if s["id"] in ids], "server_time": db.now()}

    @app.get("/api/songs/{song_id}/file")
    def song_file(song_id: int, download: int = 0):
        row = song_or_404(song_id)
        path = library.song_path(cfg, row)
        if not path.is_file():
            raise HTTPException(404, "The file for this song is missing.")
        return FileResponse(path, media_type=library.MEDIA_TYPES.get(row["ext"], "application/octet-stream"),
                            filename=safe_filename(row["title"], row["ext"]),
                            content_disposition_type="attachment" if download else "inline")

    @app.post("/api/zip")
    def zip_download(ids: str = Form(max_length=100_000)):
        try:
            wanted = [int(x) for x in ids.split(",") if x.strip()][:MAX_IDS]
        except ValueError:
            raise HTTPException(400, "ids must be a comma-separated list of song ids.")
        rows = [library.get_song(conn(), i) for i in dict.fromkeys(wanted)]
        rows = [r for r in rows if r is not None]
        if not rows:
            raise HTTPException(404, "None of the selected songs exist any more.")
        names = unique_names([safe_filename(r["title"], r["ext"]) for r in rows])
        items = [(library.song_path(cfg, r), n) for r, n in zip(rows, names)]
        stamp = time.strftime("%Y%m%d-%H%M")
        return StreamingResponse(stream_zip(items), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="music-{stamp}.zip"'})

    # ---- editor ----
    def check_edit(body: EditBody, song) -> dict:
        def rng(name, v, lo, hi):
            if not (lo <= v <= hi):
                raise HTTPException(400, f"{name} must be between {lo:g} and {hi:g}.")
        rng("Gain", body.gain, *C.GAIN_RANGE)
        rng("Target", body.target, *C.TARGET_RANGE)
        rng("Fade in", body.fade_in, *C.FADE_RANGE)
        rng("Fade out", body.fade_out, *C.FADE_RANGE)
        dur = song["duration"]
        if body.start < 0:
            raise HTTPException(400, "Start must not be negative.")
        end = body.end
        if end is not None and dur and end >= dur:
            end = None
        if end is not None and end <= body.start:
            raise HTTPException(400, "Start must be before end.")
        length = (end if end is not None else (dur or 0)) - body.start
        if dur and length <= 0:
            raise HTTPException(400, "Start must be before the end of the song.")
        if dur and body.fade_in + body.fade_out > length:
            raise HTTPException(400, "The fades are longer than the selection.")
        return {"start": round(body.start, 3), "end": None if end is None else round(end, 3),
                "gain": body.gain, "normalize": body.normalize, "target": body.target,
                "fade_in": body.fade_in, "fade_out": body.fade_out}

    def edit_job(kind: str, song_id: int, body: EditBody, request: Request):
        song = song_or_404(song_id)
        params = check_edit(body, song)
        if kind == "edit" and library.count_queued(conn()) >= cfg.max_queued_jobs:
            raise HTTPException(503, f"The queue is full ({cfg.max_queued_jobs} jobs). Try again later.")
        ip = limit(job_limiter if kind == "edit" else probe_limiter, request)
        try:
            job_id = library.create_edit_job(cfg, conn(), kind, song, params, ip)
        except library.QuotaExceeded as e:
            raise HTTPException(413, str(e))
        worker.notify(kind)
        return job_json(job_id)

    @app.post("/api/songs/{song_id}/preview", status_code=202)
    def preview(song_id: int, body: EditBody, request: Request):
        return edit_job("preview", song_id, body, request)

    @app.post("/api/songs/{song_id}/edit", status_code=202)
    def edit(song_id: int, body: EditBody, request: Request):
        return edit_job("edit", song_id, body, request)

    @app.get("/api/previews/{job_id}")
    def preview_file(job_id: int):
        row = conn().execute("SELECT * FROM jobs WHERE id = ? AND kind = 'preview' AND status = 'done'",
                             (job_id,)).fetchone()
        if row is None or not row["preview_file"]:
            raise HTTPException(404, "Preview not found (previews are deleted after a while).")
        path = cfg.preview_dir / Path(row["preview_file"]).name
        if not path.is_file():
            raise HTTPException(404, "Preview not found (previews are deleted after a while).")
        ext = path.suffix.lstrip(".")
        return FileResponse(path, media_type=library.MEDIA_TYPES.get(ext, "application/octet-stream"),
                            headers={"Cache-Control": "no-store"})

    return app
