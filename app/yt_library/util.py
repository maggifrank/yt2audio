"""URL validation and filename helpers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, urlsplit

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


class BadURL(ValueError):
    pass


def check_url(url: str) -> dict:
    """Validate a YouTube URL. Returns {url, video_id, list_id} (ids may be None).

    Only https/http URLs on youtube.com, music.youtube.com and youtu.be are accepted.
    """
    url = (url or "").strip()
    if len(url) > 500:
        raise BadURL("URL is too long")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BadURL("only http(s) YouTube URLs are accepted")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS or parts.username or parts.password or parts.port:
        raise BadURL("only youtube.com, music.youtube.com and youtu.be URLs are accepted")
    qs = parse_qs(parts.query)
    video_id = None
    if host == "youtu.be":
        video_id = parts.path.strip("/").split("/")[0] or None
    elif parts.path == "/watch":
        video_id = (qs.get("v") or [None])[0]
    else:
        m = re.match(r"^/(?:shorts|live|embed)/([^/]+)", parts.path)
        if m:
            video_id = m.group(1)
        elif parts.path != "/playlist":
            raise BadURL("URL must be a YouTube video or playlist")
    if video_id is not None and not VIDEO_ID_RE.match(video_id):
        raise BadURL("invalid YouTube video id")
    list_id = (qs.get("list") or [None])[0]
    if list_id is not None and not LIST_ID_RE.match(list_id):
        raise BadURL("invalid YouTube playlist id")
    if video_id is None and list_id is None:
        raise BadURL("URL must be a YouTube video or playlist")
    # rebuild a canonical URL so nothing else from the user's string reaches yt-dlp
    if video_id and list_id:
        canon = f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
    elif video_id:
        canon = f"https://www.youtube.com/watch?v={video_id}"
    else:
        canon = f"https://www.youtube.com/playlist?list={list_id}"
    return {"url": canon, "video_id": video_id, "list_id": list_id}


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def safe_filename(title: str, ext: str, max_len: int = 150) -> str:
    """A download-safe filename from a title (for Content-Disposition and zip entries)."""
    name = unicodedata.normalize("NFC", title or "")
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "audio"
    name = name[:max_len].rstrip(" .")
    return f"{name}.{ext}"


def unique_names(names: list[str]) -> list[str]:
    """Make names unique (case-insensitively): 'a.mp3', 'a (2).mp3', ..."""
    seen: set[str] = set()
    out = []
    for n in names:
        stem, dot, ext = n.rpartition(".")
        if not dot:
            stem, ext = n, ""
        cand, i = n, 2
        while cand.lower() in seen:
            cand = f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})"
            i += 1
        seen.add(cand.lower())
        out.append(cand)
    return out
