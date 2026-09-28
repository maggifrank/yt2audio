"""End-to-end tests with a fake yt-dlp (no network): the real yt2audio/audiocrop scripts and ffmpeg run."""

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ["PATH"] = f"{ROOT / 'tests' / 'fakebin'}:{os.environ['PATH']}"

from yt_library import cleanup, config, db  # noqa: E402
from yt_library.main import create_app  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}
PLAYLIST = "https://www.youtube.com/playlist?list=PLtest123"


@pytest.fixture
def env(tmp_path):
    e = {
        "YTL_LIBRARY_DIR": str(tmp_path / "music"),
        "YTL_DB_PATH": str(tmp_path / "db" / "library.db"),
        "YTL_YT2AUDIO": str(ROOT / "bin" / "yt2audio"),
        "YTL_AUDIOCROP": str(ROOT / "bin" / "audiocrop"),
        "YTL_YTDLP": str(ROOT / "tests" / "fakebin" / "yt-dlp"),
        "YTL_LIFETIMES": "1h,1d,7d,30d,90d",
        "YTL_MAX_LIFETIME": "30d",
        "YTL_RATE_LIMIT": "100/1h",
        "YTL_TRUSTED_PROXIES": "10.0.0.5",
    }
    return e


@pytest.fixture
def client(env):
    cfg = config.load(env)
    with TestClient(create_app(cfg)) as c:
        c.cfg = cfg
        yield c


def wait_job(c, job_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {j}")


def test_config_drops_lifetimes_over_max(client):
    cfg = client.get("/api/config").json()
    assert [l["key"] for l in cfg["lifetimes"]] == ["1h", "1d", "7d", "30d"]
    assert cfg["edit"]["gain_max"] == 20


def test_rejects_non_youtube_and_cross_origin(client):
    r = client.post("/api/probe", json={"url": "https://evil.example/watch?v=AAAAAAAAAAA"}, headers=ORIGIN)
    assert r.status_code == 400
    r = client.post("/api/probe", json={"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/probe", json={"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"})
    assert r.status_code == 403  # no Origin/Referer at all


def test_probe_playlist_and_video_in_playlist(client):
    r = client.post("/api/probe", json={"url": "https://youtube.com/watch?v=BBBBBBBBBBB&list=PLtest123&t=5"},
                    headers=ORIGIN)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["is_playlist"] and p["track_count"] == 4
    assert p["video_in_playlist"]["video_id"] == "BBBBBBBBBBB"
    assert [e["too_long"] for e in p["entries"]] == [False, False, False, True]


def test_playlist_download_dedupe_zip_edit_cleanup(client):
    c = client
    r = c.post("/api/jobs", json={"url": PLAYLIST, "format": "mp3", "lifetime": "1h"}, headers=ORIGIN)
    assert r.status_code == 202, r.text
    job = wait_job(c, r.json()["id"])
    st = {t["video_id"]: t["status"] for t in job["tracks"]}
    assert job["status"] == "partial"
    assert st == {"AAAAAAAAAAA": "done", "BBBBBBBBBBB": "done", "XXXXXXXXXXX": "failed", "LLLLLLLLLLL": "skipped"}
    failed = next(t for t in job["tracks"] if t["status"] == "failed")
    assert "Private video" in failed["error"]
    songs = c.get("/api/songs").json()["songs"]
    assert len(songs) == 2
    lib = Path(c.cfg.library_dir)
    assert sorted(p.name for p in lib.iterdir() if not p.name.startswith(".")) == \
        ["AAAAAAAAAAA.mp3", "BBBBBBBBBBB.mp3"]
    a = next(s for s in songs if s["video_id"] == "AAAAAAAAAAA")
    old_exp = a["expires_at"]

    # duplicate: same video + format is not downloaded again, expiry extended
    r = c.post("/api/jobs", json={"url": "https://youtu.be/AAAAAAAAAAA", "format": "mp3", "lifetime": "7d"},
               headers=ORIGIN)
    job = wait_job(c, r.json()["id"])
    assert job["status"] == "done" and job["tracks"][0]["status"] == "duplicate"
    assert job["tracks"][0]["song_id"] == a["id"]
    assert c.get(f"/api/songs/{a['id']}").json()["expires_at"] > old_exp + 86400

    # single video out of a playlist, other format (alac and m4a must not collide)
    for fmt in ("alac", "m4a", "vorbis"):
        r = c.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB&list=PLtest123",
                                      "format": fmt, "single": True}, headers=ORIGIN)
        job = wait_job(c, r.json()["id"])
        assert job["status"] == "done" and len(job["tracks"]) == 1, job
    names = sorted(p.name for p in lib.iterdir() if not p.name.startswith("."))
    assert "BBBBBBBBBBB-alac.m4a" in names and "BBBBBBBBBBB.m4a" in names and "BBBBBBBBBBB.ogg" in names

    # file serving with Range, and Content-Disposition from the title
    r = c.get(f"/api/songs/{a['id']}/file", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    r = c.get(f"/api/songs/{a['id']}/file?download=1")
    assert "attachment" in r.headers["content-disposition"]
    assert "/" not in r.headers["content-disposition"].split("filename")[1]

    # zip: titles are identical for A's formats? use all songs; names sanitized and unique
    songs = c.get("/api/songs").json()["songs"]
    ids = ",".join(str(s["id"]) for s in songs)
    r = c.post("/api/zip", data={"ids": ids}, headers=ORIGIN)
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(zf.namelist()) == len(songs) == len(set(n.lower() for n in zf.namelist()))
    assert all("/" not in n for n in zf.namelist())
    assert zf.testzip() is None

    # editor: validation, preview and save
    r = c.post(f"/api/songs/{a['id']}/edit", json={"gain": 25}, headers=ORIGIN)
    assert r.status_code == 400
    r = c.post(f"/api/songs/{a['id']}/edit", json={"start": 1, "end": 2, "fade_in": 1, "fade_out": 1.5},
               headers=ORIGIN)
    assert r.status_code == 400 and "fades" in r.json()["detail"]
    r = c.post(f"/api/songs/{a['id']}/preview", json={"start": 0.5, "gain": 6, "normalize": True}, headers=ORIGIN)
    pj = wait_job(c, r.json()["id"])
    assert pj["status"] == "done", pj
    assert c.get(pj["preview_url"]).status_code == 200
    r = c.post(f"/api/songs/{a['id']}/edit", json={"start": 0.5, "end": 2.5, "gain": 3, "fade_out": 0.5},
               headers=ORIGIN)
    ej = wait_job(c, r.json()["id"])
    assert ej["status"] == "done", ej
    new = c.get(f"/api/songs/{ej['result_song_id']}").json()
    assert new["title"].endswith("(edit)") and new["parent_id"] == a["id"]
    assert new["expires_at"] == c.get(f"/api/songs/{a['id']}").json()["expires_at"]
    assert abs(new["duration"] - 2.0) < 0.1
    assert (lib / "AAAAAAAAAAA-edit-1.mp3").is_file()

    # cleanup: expire A, add an orphan and a missing-file row; cleanup twice is safe
    conn = db.connect(c.cfg.db_path)
    conn.execute("UPDATE songs SET expires_at = 1 WHERE id = ?", (a["id"],))
    (lib / "orphan.mp3").write_bytes(b"x")
    os.utime(lib / "orphan.mp3", (1, 1))
    b = next(s for s in songs if s["video_id"] == "BBBBBBBBBBB" and s["format"] == "mp3")
    (lib / "BBBBBBBBBBB.mp3").unlink()
    conn.execute("UPDATE songs SET created_at = 1 WHERE id = ?", (b["id"],))
    conn.execute("UPDATE jobs SET status='running', heartbeat_at=1 WHERE id = ?", (ej["id"],))
    stats = cleanup.run(c.cfg)
    assert stats["expired"] == 1 and stats["orphans"] == 1 and stats["missing"] == 1 and stats["stuck"] == 1
    assert not (lib / "AAAAAAAAAAA.mp3").exists() and not (lib / "orphan.mp3").exists()
    assert (lib / "AAAAAAAAAAA-edit-1.mp3").exists()  # the edit keeps its own (inherited) expiry
    assert cleanup.run(c.cfg)["expired"] == 0

    # delete
    r = c.delete(f"/api/songs/{new['id']}", headers=ORIGIN)
    assert r.status_code == 204 and not (lib / "AAAAAAAAAAA-edit-1.mp3").exists()


def test_quota_rejects_instead_of_deleting(env):
    env = {**env, "YTL_STORAGE_QUOTA": "100K"}
    with TestClient(create_app(config.load(env))) as c:
        r = c.post("/api/jobs", json={"url": PLAYLIST, "format": "wav"}, headers=ORIGIN)
        assert r.status_code == 413 and "storage" in r.json()["detail"].lower()


def test_rate_limit_uses_forwarded_for_only_from_trusted_proxy(env):
    env = {**env, "YTL_RATE_LIMIT": "2/1h"}
    with TestClient(create_app(config.load(env)), client=("10.0.0.5", 5000)) as c:
        body = {"url": "https://www.youtube.com/watch?v=XXXXXXXXXXX"}
        h1 = {**ORIGIN, "X-Forwarded-For": "192.168.1.10"}
        h2 = {**ORIGIN, "X-Forwarded-For": "192.168.1.11"}
        codes = [c.post("/api/probe", json=body, headers=h1).status_code for _ in range(9)]
        assert codes[-1] == 429
        assert c.post("/api/probe", json=body, headers=h2).status_code == 200
    with TestClient(create_app(config.load(env)), client=("10.9.9.9", 5000)) as c:
        # untrusted peer: X-Forwarded-For is ignored, so spoofing a new IP does not help
        codes = [c.post("/api/probe", json={"url": "https://www.youtube.com/watch?v=XXXXXXXXXXX"},
                        headers={**ORIGIN, "X-Forwarded-For": f"1.2.3.{i}"}).status_code for i in range(9)]
        assert codes[-1] == 429


def _max_overlap(trace: Path) -> int:
    events = []
    for line in trace.read_text().splitlines():
        kind, t = line.split()
        events.append((float(t), 1 if kind == "start" else -1))
    cur = best = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):
        cur += d
        best = max(best, cur)
    return best


@pytest.mark.parametrize("parallel", [1, 3])
def test_parallel_downloads_respect_limit(env, tmp_path, monkeypatch, parallel):
    trace = tmp_path / "trace"
    monkeypatch.setenv("FAKE_YTDLP_TRACE", str(trace))
    monkeypatch.setenv("FAKE_YTDLP_SLEEP", "1")
    env = {**env, "YTL_PARALLEL_DOWNLOADS": str(parallel)}
    with TestClient(create_app(config.load(env))) as c:
        # one playlist (2 downloadable tracks) plus two single videos in other formats
        jobs = [c.post("/api/jobs", json={"url": PLAYLIST, "format": "mp3"}, headers=ORIGIN).json()["id"]]
        for fmt in ("opus", "flac"):
            jobs.append(c.post("/api/jobs", json={"url": "https://youtu.be/AAAAAAAAAAA", "format": fmt},
                               headers=ORIGIN).json()["id"])
        # the same song in the same format twice at once is downloaded only once
        jobs.append(c.post("/api/jobs", json={"url": "https://youtu.be/AAAAAAAAAAA", "format": "mp3"},
                           headers=ORIGIN).json()["id"])
        results = [wait_job(c, j) for j in jobs]
        assert [r["status"] for r in results] == ["partial", "done", "done", "done"]
        a_mp3 = [t for r in results for t in r["tracks"] if t["video_id"] == "AAAAAAAAAAA"
                 and r["format"] == "mp3"]
        assert sorted(t["status"] for t in a_mp3) == ["done", "duplicate"]
        assert len({t["song_id"] for t in a_mp3}) == 1
    overlap = _max_overlap(trace)
    assert overlap == parallel if parallel == 1 else 2 <= overlap <= parallel


def test_transient_failure_is_retried_automatically(env, tmp_path, monkeypatch):
    from yt_library import worker
    monkeypatch.setattr(worker, "AUTO_RETRY_DELAY", 0)
    monkeypatch.setenv("FAKE_YTDLP_STATE", str(tmp_path))
    with TestClient(create_app(config.load(env))) as c:
        r = c.post("/api/jobs", json={"url": "https://youtu.be/FFFFFFFFFFF", "format": "mp3"}, headers=ORIGIN)
        job = wait_job(c, r.json()["id"])
        assert job["status"] == "done" and job["tracks"][0]["status"] == "done", job


def test_manual_retry_of_failed_tracks(env, tmp_path, monkeypatch):
    from yt_library import worker
    monkeypatch.setattr(worker, "AUTO_RETRY_DELAY", 0)
    with TestClient(create_app(config.load(env))) as c:
        r = c.post("/api/jobs", json={"url": PLAYLIST, "format": "mp3"}, headers=ORIGIN)
        job = wait_job(c, r.json()["id"])
        assert job["status"] == "partial"
        # a private video is not retried automatically, but can be retried by hand
        x = next(t for t in job["tracks"] if t["video_id"] == "XXXXXXXXXXX")
        assert x["status"] == "failed"
        r = c.post(f"/api/jobs/{job['id']}/retry", json={}, headers=ORIGIN)
        assert r.status_code == 202, r.text
        again = wait_job(c, job["id"])
        assert again["status"] == "partial"
        assert next(t for t in again["tracks"] if t["video_id"] == "XXXXXXXXXXX")["status"] == "failed"
        # skipped (too long) tracks are not retried, and done tracks stay done
        st = {t["video_id"]: t["status"] for t in again["tracks"]}
        assert st["AAAAAAAAAAA"] == "done" and st["LLLLLLLLLLL"] == "skipped"
        # a job without failures can't be retried
        r = c.post("/api/jobs", json={"url": "https://youtu.be/AAAAAAAAAAA", "format": "opus"}, headers=ORIGIN)
        ok = wait_job(c, r.json()["id"])
        assert c.post(f"/api/jobs/{ok['id']}/retry", json={}, headers=ORIGIN).status_code == 409
        # retrying one specific track
        r = c.post(f"/api/jobs/{job['id']}/retry", json={"tracks": [3]}, headers=ORIGIN)
        assert r.status_code == 202
        assert wait_job(c, job["id"])["status"] == "partial"


def test_new_edit_after_deleting_one_gets_a_new_id_and_exact_crop(client):
    c = client
    job = wait_job(c, c.post("/api/jobs", json={"url": "https://youtu.be/AAAAAAAAAAA", "format": "mp3"},
                             headers=ORIGIN).json()["id"])
    src = job["tracks"][0]["song_id"]
    first = wait_job(c, c.post(f"/api/songs/{src}/edit", json={"start": 1, "end": 2}, headers=ORIGIN).json()["id"])
    assert c.delete(f"/api/songs/{first['result_song_id']}", headers=ORIGIN).status_code == 204
    second = wait_job(c, c.post(f"/api/songs/{src}/edit", json={"start": 1.2, "end": 2.7},
                                headers=ORIGIN).json()["id"])
    assert second["result_song_id"] != first["result_song_id"]  # ids are never reused
    song = c.get(f"/api/songs/{second['result_song_id']}").json()
    assert abs(song["duration"] - 1.5) < 0.06, song["duration"]
    r = c.get(song["download_url"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_iphone_ringtone_export(client):
    c = client
    job = wait_job(c, c.post("/api/jobs", json={"url": "https://youtu.be/RRRRRRRRRRR", "format": "mp3"},
                             headers=ORIGIN).json()["id"])
    src = job["tracks"][0]["song_id"]
    # the whole 50 s song is too long for a ringtone
    r = c.post(f"/api/songs/{src}/edit", json={"ringtone": True}, headers=ORIGIN)
    assert r.status_code == 400 and "40 seconds" in r.json()["detail"]
    pj = wait_job(c, c.post(f"/api/songs/{src}/preview", json={"start": 5, "end": 35, "ringtone": True},
                            headers=ORIGIN).json()["id"])
    assert pj["status"] == "done" and c.get(pj["preview_url"]).status_code == 200
    ej = wait_job(c, c.post(f"/api/songs/{src}/edit", json={"start": 5, "end": 35, "fade_out": 2, "ringtone": True},
                            headers=ORIGIN).json()["id"])
    assert ej["status"] == "done", ej
    song = c.get(f"/api/songs/{ej['result_song_id']}").json()
    assert song["ext"] == "m4r" and song["title"].endswith("(ringtone)") and abs(song["duration"] - 30) < 0.1
    r = c.get(song["download_url"])
    assert r.status_code == 200 and ".m4r" in r.headers["content-disposition"]
    assert r.content[4:8] == b"ftyp"  # MP4 container, as iPhones expect


def test_edit_can_change_format(client):
    c = client
    job = wait_job(c, c.post("/api/jobs", json={"url": "https://youtu.be/AAAAAAAAAAA", "format": "mp3"},
                             headers=ORIGIN).json()["id"])
    src = job["tracks"][0]["song_id"]
    assert c.post(f"/api/songs/{src}/edit", json={"output": "exe"}, headers=ORIGIN).status_code == 400
    pj = wait_job(c, c.post(f"/api/songs/{src}/preview", json={"output": "wav"}, headers=ORIGIN).json()["id"])
    assert pj["status"] == "done" and c.get(pj["preview_url"]).headers["content-type"] == "audio/wav"
    for out, ext, fmt in (("wav", "wav", "wav"), ("flac", "flac", "flac"), ("ogg", "ogg", "vorbis"),
                          ("mp3", "mp3", "mp3"), ("same", "mp3", "mp3")):
        ej = wait_job(c, c.post(f"/api/songs/{src}/edit", json={"start": 0.5, "output": out},
                                headers=ORIGIN).json()["id"])
        assert ej["status"] == "done", ej
        song = c.get(f"/api/songs/{ej['result_song_id']}").json()
        assert (song["ext"], song["format"]) == (ext, fmt)
        assert song["title"] == ("Song A (edit)" if ext == "mp3" else f"Song A (edit, {ext.upper()})")
        r = c.get(song["download_url"])
        assert r.status_code == 200 and f".{ext}" in r.headers["content-disposition"]
    wav = next(s for s in c.get("/api/songs").json()["songs"] if s["ext"] == "wav")
    assert c.get(wav["download_url"]).content[:4] == b"RIFF"
