"""Subprocess wrappers: yt-dlp (probe), yt2audio (download), audiocrop (edit), ffprobe.

Every call uses an argument list (never a shell) and runs in its own process
group so a shutdown can stop the whole tree (yt-dlp -> ffmpeg, audiocrop -> ffmpeg).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_running: set[subprocess.Popen] = set()
_lock = threading.Lock()


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run(args: list[str], timeout: float, cwd: Path | None = None) -> Result:
    proc = subprocess.Popen(
        [str(a) for a in args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", cwd=cwd, start_new_session=True,
    )
    with _lock:
        _running.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return Result(proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _killpg(proc)
        out, err = proc.communicate()
        return Result(-1, out, err, timed_out=True)
    finally:
        with _lock:
            _running.discard(proc)


def _killpg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def kill_all() -> None:
    with _lock:
        procs = list(_running)
    for p in procs:
        _killpg(p)


def last_error(stderr: str, default: str) -> str:
    """The most useful line of a tool's stderr for showing to users."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ERROR:"):
            return ln[len("ERROR:"):].strip()[:500]
    for ln in reversed(lines):
        if ln.startswith("audiocrop:"):
            return ln[len("audiocrop:"):].strip()[:500]
    return (lines[-1][:500] if lines else default)


# ---- probe ------------------------------------------------------------------

class ProbeError(Exception):
    pass


def _entry(e: dict, index: int) -> dict | None:
    vid = e.get("id")
    if not isinstance(vid, str) or len(vid) != 11:
        return None
    dur = e.get("duration")
    return {
        "index": index,
        "video_id": vid,
        "title": str(e.get("title") or vid),
        "uploader": e.get("uploader") or e.get("channel") or None,
        "duration": float(dur) if isinstance(dur, (int, float)) else None,
    }


def probe(cfg: Config, url: str, max_entries: int) -> dict:
    """`yt-dlp --flat-playlist -J`: title, is_playlist and entries (index, video_id, title, ...)."""
    res = run([cfg.ytdlp, "--flat-playlist", "-J", "--no-warnings", "--yes-playlist",
               "--playlist-end", str(max_entries + 1), "--", url], timeout=180)
    if res.timed_out:
        raise ProbeError("looking up the URL timed out")
    try:
        info = json.loads(res.stdout) if res.stdout.strip() else None
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict):
        raise ProbeError(last_error(res.stderr, "yt-dlp could not read this URL"))
    if info.get("_type") == "playlist" or "entries" in info:
        entries = []
        for i, e in enumerate(info.get("entries") or [], start=1):
            if isinstance(e, dict):
                ent = _entry(e, i)
                if ent:
                    entries.append(ent)
        return {"title": str(info.get("title") or "Playlist"), "is_playlist": True, "entries": entries}
    ent = _entry(info, 1)
    if not ent:
        raise ProbeError("yt-dlp returned no video for this URL")
    return {"title": ent["title"], "is_playlist": False, "entries": [ent]}


# ---- yt2audio -----------------------------------------------------------------

def download(cfg: Config, url: str, fmt: str, outdir: Path, timeout: float) -> tuple[list[Path], Result]:
    """Run `yt2audio -1 -q <url> <outdir> <format>`; returns the created files that exist."""
    res = run([cfg.yt2audio, "-1", "-q", url, str(outdir), fmt], timeout=timeout, cwd=outdir)
    files = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = Path(line)
        if not p.is_absolute():
            p = outdir / p
        try:
            p = p.resolve()
            p.relative_to(outdir.resolve())
        except (OSError, ValueError):
            continue
        if p.is_file():
            files.append(p)
    return files, res


# ---- audiocrop ----------------------------------------------------------------

def audiocrop(cfg: Config, src: Path, dst: Path, params: dict, preview: int | None, timeout: float) -> Result:
    args = [cfg.audiocrop, "-q", "-f", "-s", f"{params['start']:.3f}"]
    if params.get("end") is not None:
        args += ["-e", f"{params['end']:.3f}"]
    args += ["-g", _num(params["gain"])]
    if params.get("normalize"):
        args += ["-n", "-t", _num(params["target"])]
    args += ["--fade-in", _num(params["fade_in"]), "--fade-out", _num(params["fade_out"])]
    if preview:
        args += ["-p", str(preview)]
    args += ["--", str(src), str(dst)]
    return run(args, timeout=timeout)


def _num(v: float) -> str:
    # audiocrop accepts -?[0-9]+(\.[0-9]+)? ; never emit exponents or "-0.0"
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ---- ffprobe ------------------------------------------------------------------

def ffprobe(path: Path) -> dict:
    res = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=title,artist",
               "-of", "json", str(path)], timeout=60)
    try:
        fmt = json.loads(res.stdout).get("format", {})
    except (json.JSONDecodeError, AttributeError):
        return {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    try:
        dur = float(fmt.get("duration"))
    except (TypeError, ValueError):
        dur = None
    return {"duration": dur, "title": tags.get("title"), "artist": tags.get("artist")}
