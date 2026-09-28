"""Configuration from YTL_* environment variables (see deploy/yt-library.env.example)."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

FORMATS = ["mp3", "m4a", "opus", "vorbis", "wav", "flac", "aac", "alac"]
# yt-dlp names vorbis files .ogg and alac files .m4a
FORMAT_EXT = {"mp3": "mp3", "m4a": "m4a", "opus": "opus", "vorbis": "ogg",
              "wav": "wav", "flac": "flac", "aac": "aac", "alac": "m4a"}
# rough bytes per second of audio, used to estimate job size for the quota check
FORMAT_BYTES_PER_SEC = {"mp3": 32_000, "m4a": 32_000, "opus": 20_000, "vorbis": 32_000,
                        "aac": 32_000, "wav": 176_400, "flac": 110_000, "alac": 110_000}

# audiocrop's own limits; the backend and UI use the same ranges
GAIN_RANGE = (-20.0, 20.0)
TARGET_RANGE = (-30.0, -5.0)
TARGET_DEFAULT = -14.0
FADE_RANGE = (0.0, 10.0)
PREVIEW_SECONDS = 15
RINGTONE_MAX_SECONDS = 40  # iPhone limit; audiocrop enforces it for .m4r

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_UNIT_NAMES = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week"}
_SIZES = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


class ConfigError(ValueError):
    pass


def parse_duration(value: str) -> int:
    """'90' -> 90, '15m' -> 900, '1d' -> 86400."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value.lower())
    if not m:
        raise ConfigError(f"invalid duration {value!r} (use e.g. 90, 15m, 1h, 7d)")
    return int(m.group(1)) * _UNITS[m.group(2) or "s"]


def duration_label(value: str) -> str:
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value.lower())
    if not m:
        return value
    n, unit = int(m.group(1)), _UNIT_NAMES[m.group(2) or "s"]
    return f"{n} {unit}{'' if n == 1 else 's'}"


def parse_size(value: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([bkmgt]?)i?b?\s*", value.lower())
    if not m:
        raise ConfigError(f"invalid size {value!r} (use e.g. 500M, 20G)")
    return int(float(m.group(1)) * _SIZES[m.group(2)])


@dataclass(frozen=True)
class Lifetime:
    key: str
    label: str
    seconds: int


@dataclass
class Config:
    library_dir: Path
    db_path: Path
    host: str
    port: int
    lifetimes: list[Lifetime]
    default_lifetime: str
    max_lifetime: int
    max_tracks_per_job: int
    max_track_duration: int
    max_queued_jobs: int
    parallel_downloads: int
    storage_quota: int
    rate_limit_count: int
    rate_limit_window: int
    trusted_proxies: list = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    preview_ttl: int = 1800
    job_timeout: int = 10800
    yt2audio: str = "/usr/local/bin/yt2audio"
    audiocrop: str = "/usr/local/bin/audiocrop"
    ytdlp: str = "/usr/local/bin/yt-dlp"

    @property
    def tmp_dir(self) -> Path:
        return self.library_dir / ".tmp"

    @property
    def preview_dir(self) -> Path:
        return self.library_dir / ".previews"

    def lifetime(self, key: str) -> Lifetime:
        for lt in self.lifetimes:
            if lt.key == key:
                return lt
        raise ConfigError(f"unknown lifetime {key!r}")


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    def get(name: str, default: str) -> str:
        v = env.get(name, "").strip()
        return v if v else default

    max_lifetime = parse_duration(get("YTL_MAX_LIFETIME", "30d"))
    lifetimes = []
    for key in get("YTL_LIFETIMES", "1h,1d,7d,30d").split(","):
        key = key.strip().lower()
        if not key:
            continue
        secs = parse_duration(key)
        if secs <= 0:
            raise ConfigError(f"lifetime {key!r} must be positive")
        if secs > max_lifetime:
            continue  # options above the max are simply not offered
        lifetimes.append(Lifetime(key, duration_label(key), secs))
    if not lifetimes:
        raise ConfigError("YTL_LIFETIMES has no option within YTL_MAX_LIFETIME")
    lifetimes.sort(key=lambda lt: lt.seconds)
    default_lifetime = get("YTL_DEFAULT_LIFETIME", "1d").lower()
    if default_lifetime not in {lt.key for lt in lifetimes}:
        default_lifetime = lifetimes[0].key

    rate = get("YTL_RATE_LIMIT", "30/1h")
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\S+)\s*", rate)
    if not m:
        raise ConfigError(f"invalid YTL_RATE_LIMIT {rate!r} (use e.g. 30/1h)")

    proxies = []
    for p in get("YTL_TRUSTED_PROXIES", "127.0.0.1,::1").split(","):
        if p.strip():
            proxies.append(ipaddress.ip_network(p.strip(), strict=False))

    origins = [o.strip().rstrip("/").lower() for o in get("YTL_ALLOWED_ORIGINS", "").split(",") if o.strip()]

    return Config(
        library_dir=Path(get("YTL_LIBRARY_DIR", "/srv/music")),
        db_path=Path(get("YTL_DB_PATH", "/var/lib/yt-library/library.db")),
        host=get("YTL_HOST", "127.0.0.1"),
        port=int(get("YTL_PORT", "8080")),
        lifetimes=lifetimes,
        default_lifetime=default_lifetime,
        max_lifetime=max_lifetime,
        max_tracks_per_job=int(get("YTL_MAX_TRACKS_PER_JOB", "100")),
        max_track_duration=parse_duration(get("YTL_MAX_TRACK_DURATION", "2h")),
        max_queued_jobs=int(get("YTL_MAX_QUEUED_JOBS", "20")),
        parallel_downloads=max(1, min(10, int(get("YTL_PARALLEL_DOWNLOADS", "3")))),
        storage_quota=parse_size(get("YTL_STORAGE_QUOTA", "20G")),
        rate_limit_count=int(m.group(1)),
        rate_limit_window=parse_duration(m.group(2)),
        trusted_proxies=proxies,
        allowed_origins=origins,
        preview_ttl=parse_duration(get("YTL_PREVIEW_TTL", "30m")),
        job_timeout=parse_duration(get("YTL_JOB_TIMEOUT", "3h")),
        yt2audio=get("YTL_YT2AUDIO", "/usr/local/bin/yt2audio"),
        audiocrop=get("YTL_AUDIOCROP", "/usr/local/bin/audiocrop"),
        ytdlp=get("YTL_YTDLP", "/usr/local/bin/yt-dlp"),
    )
