# yt2audio / yt-library

A small self-hosted web app that turns two bash scripts into a **shared, temporary
music library**: paste a YouTube video or playlist URL, the audio is downloaded in
the background, everyone on the network sees the same library, songs expire
automatically, and you can crop/boost songs and download selections as a zip.

There are no user accounts. It is meant to run in a Debian LXC behind Caddy,
reachable only on an internal network or tailnet.

- `bin/yt2audio`: download audio from YouTube (yt-dlp).
- `bin/audiocrop`: crop, boost, normalize and fade an audio file (ffmpeg).
- `app/`: the web app (FastAPI + SQLite, vanilla HTML/CSS/JS, no build step).
- `install.sh` / `update.sh`: installer and updater for a fresh Debian LXC.
- `deploy/`: systemd units, config example, Caddyfile example, version pins.

## Secrets

This repository is public. Never commit secrets: `.env`, API keys,
credentials, private keys and cookies files are all ignored by `.gitignore`.
Real configuration lives in `/etc/yt-library/yt-library.env` on the server;
`deploy/yt-library.env.example` holds placeholders only.

## Setup (Debian 12/13 LXC)

```sh
apt-get install -y git
git clone https://github.com/maggifrank/yt2audio.git /opt/yt2audio-src
cd /opt/yt2audio-src
sudo ./install.sh
```

`install.sh` is idempotent (re-run it any time). It:

1. installs `ffmpeg`, `python3`, `python3-venv`, `curl`, `unzip`;
2. creates the unprivileged system user `yt-library`;
3. copies `deploy/yt-library.env.example` to `/etc/yt-library/yt-library.env` (only if missing);
4. installs **yt-dlp** into its own venv `/opt/yt-dlp` at the version pinned in
   `deploy/yt-dlp.version` (`yt-dlp[default]`, not apt), linked as `/usr/local/bin/yt-dlp`;
5. installs **deno** (pinned in `deploy/deno.version`, checksum-verified): current
   yt-dlp needs a JavaScript runtime to download from YouTube;
6. installs `yt2audio` and `audiocrop` to `/usr/local/bin` (the web app and the
   command line use the same copies);
7. installs the app into `/opt/yt-library/venv` (pinned `app/requirements.txt`) and a
   `yt-library` wrapper in `/usr/local/bin` (it drops to the `yt-library` user when run as root);
8. creates the library dir (default `/srv/music`, owner `yt-library`, mode 0750, so only
   the service user can write) and `/var/lib/yt-library` (SQLite database);
9. installs and starts `yt-library.service` and `yt-library-cleanup.timer`.

Proxmox: the systemd sandboxing in the units needs `nesting=1` on the container (the
default for new unprivileged containers).

### Caddy

The app listens on `YTL_HOST:YTL_PORT` (default `127.0.0.1:8080`); Caddy does TLS.
See `deploy/Caddyfile.example`. If Caddy runs on another host:

- set `YTL_HOST` to the LXC's internal IP (never `0.0.0.0` on an exposed interface);
- set `YTL_TRUSTED_PROXIES` to the Caddy host's IP, so `X-Forwarded-For` is trusted
  from it (and only from it) for per-IP rate limiting;
- if the site is reached under more than one hostname, list the extra origins in
  `YTL_ALLOWED_ORIGINS`.

Logs: `journalctl -u yt-library -f`, cleanup: `journalctl -u yt-library-cleanup`.

## Configuration

Edit `/etc/yt-library/yt-library.env`, then `systemctl restart yt-library`
(re-run `install.sh` after changing paths or the cleanup schedule, since it writes the
systemd drop-ins from them). Durations use `s m h d w`, sizes `K M G T`.

| Variable | Default | Meaning |
|---|---|---|
| `YTL_LIBRARY_DIR` | `/srv/music` | Library files; also holds `.tmp/` (job dirs) and `.previews/` |
| `YTL_DB_PATH` | `/var/lib/yt-library/library.db` | SQLite database (WAL mode); keep on local disk |
| `YTL_HOST` / `YTL_PORT` | `127.0.0.1` / `8080` | Listen address |
| `YTL_LIFETIMES` | `1h,1d,7d,30d` | Lifetime options shown in the UI (options above the max are dropped) |
| `YTL_DEFAULT_LIFETIME` | `1d` | Preselected lifetime |
| `YTL_MAX_LIFETIME` | `30d` | Nothing, including extensions, can live longer than this from now |
| `YTL_CLEANUP_ONCALENDAR` | `*:0/15` | systemd `OnCalendar` for the cleanup timer |
| `YTL_MAX_TRACKS_PER_JOB` | `100` | Larger playlists are rejected |
| `YTL_MAX_TRACK_DURATION` | `2h` | Longer tracks are skipped (shown per track) |
| `YTL_MAX_QUEUED_JOBS` | `20` | New jobs get "queue full" beyond this |
| `YTL_PARALLEL_DOWNLOADS` | `3` | Songs downloaded (or edits rendered) at the same time, 1 to 10 |
| `YTL_STORAGE_QUOTA` | `20G` | Total library size; jobs that would exceed it are rejected |
| `YTL_RATE_LIMIT` | `30/1h` | Job submissions per client IP per window (probes/previews get 4x) |
| `YTL_TRUSTED_PROXIES` | `127.0.0.1` | IPs/CIDRs whose `X-Forwarded-For` is trusted |
| `YTL_ALLOWED_ORIGINS` | (empty) | Extra origins allowed to make POST/DELETE requests |
| `YTL_PREVIEW_TTL` | `30m` | Editor previews are deleted after this |
| `YTL_JOB_TIMEOUT` | `3h` | Running jobs without progress for this long are marked failed |
| `YTL_YT2AUDIO` / `YTL_AUDIOCROP` / `YTL_YTDLP` | `/usr/local/bin/...` | Tool paths |

## Using it

- **Add music**: paste a URL, pick format and lifetime, press *Check* to see the track
  count and titles (from `yt-dlp --flat-playlist -J`), choose *Just this video* when the
  URL is a video inside a playlist, then *Download*.
- **Jobs** run in the background, several songs at a time (`YTL_PARALLEL_DOWNLOADS`); each track shows done / failed (with the
  reason) / skipped / duplicate. You can close the page and come back.
- **Retries**: when a playlist job finishes, tracks that failed for a reason that might
  be temporary (such as YouTube's intermittent `HTTP Error 403: Forbidden`) are retried
  once automatically after 20 seconds. Private, removed, age- or region-restricted videos
  are not. Any finished job with failed tracks also has a *Retry failed tracks* button
  (and a *Retry* link per track) that re-queues them.
- **Library**: search, sort, play in the browser, extend, delete (with confirmation),
  select (or *select all matching filter*) and download a zip (streamed, not built on disk).
  Songs expiring within 24 hours are highlighted. Edits show which song they came from.
- **Duplicates**: a video already in the library in the same format is not downloaded
  again; the existing song's expiry is extended if the new lifetime is longer, and the
  job links to it.
- **Edit** opens the crop & boost editor: waveform (wavesurfer.js from jsDelivr) with a
  draggable region, typed start/end times (`m:ss.mmm`), *Play selection*, gain,
  normalize (+ target LUFS under *Advanced*), fades, a server-rendered 15-second
  *Preview*, and *Save as new song*, which keeps the original and creates
  "<title> (edit)" in the same format with the original's expiry.
- **Save as another format**: the editor's *Save as* menu keeps the original's format by
  default, or saves the edit as WAV, FLAC, MP3, M4A, Opus or Ogg Vorbis (for example a WAV
  of a song you downloaded as mp3 by mistake; it can't restore quality mp3 already lost).
- **iPhone ringtones**: in the editor, set *Save as* to *iPhone ringtone*, select at most
  40 seconds (the iPhone limit), then save and download the `.m4r` (AAC). The server can't
  put it on the phone: on a Mac, connect the iPhone and drag the file onto it in Finder
  (on Windows use the Apple Devices app or iTunes), then choose it under Settings >
  Sounds & Haptics > Ringtone. Without a computer, GarageBand on the iPhone can import the
  file and export it as a ringtone.

## The scripts

Both are installed to `/usr/local/bin` and work on their own (Linux and macOS).

### yt2audio

```
yt2audio [-1] [-q] <youtube-url> [output-dir] [format]
yt2audio [-1] [-q] <youtube-url> [format]
  -1  fetch only the video, even if the URL contains a playlist
  -q  quiet: print only the final file path(s)
```

Formats: `mp3 m4a opus vorbis wav flac aac alac` (default mp3; vorbis files are named
`.ogg`, alac files `.m4a`). Whole playlists are downloaded by default; unavailable
videos are skipped (`--ignore-errors`), so the exit code can be non-zero even when
some tracks succeeded.

### audiocrop

```
audiocrop [options] <input> <output>
  -s, --start TIME      crop start (default: 0)
  -e, --end TIME        crop end (default: end of file)
  -g, --gain DB         gain in dB, -20 to 20 (default: 0)
  -n, --normalize       loudness-normalize (EBU R128 / loudnorm)
  -t, --target LUFS     normalize target, -30 to -5 (default: -14)
      --fade-in SEC     fade in length, 0 to 10 (default: 0)
      --fade-out SEC    fade out length, 0 to 10 (default: 0)
  -p, --preview SEC     render only the first SEC seconds, 1 to 60
  -f, --force           overwrite the output file if it exists
  -q, --quiet           print only the output path
```

TIME is `75.5`, `1:15.5` or `0:01:15.5`. Filter order: trim → normalize → gain → fades →
limiter (always on, -1 dBFS ceiling). Output format follows the output extension
(`mp3 m4a aac opus ogg wav flac m4r`); ALAC input written to `.m4a` stays ALAC.
`.m4r` writes an iPhone ringtone (AAC in an MP4 container) and refuses clips over 40 seconds.
Exit codes: 0 ok, 1 usage error, 2 invalid input, 3 ffmpeg failed; 130/143 when
interrupted.

**Fix in this repo:** previously, Ctrl-C or `kill` did not stop audiocrop: bash
deferred the trap until the foreground ffmpeg finished, so the encode ran to the end,
the trap removed the temp file, and `mv` then failed with exit code 1. ffmpeg now
runs in the background while the script `wait`s, so the signal is handled at once:
ffmpeg is stopped, the temp file removed, and the script exits 130 (INT) or 143 (TERM).

## How the app uses the scripts

- **Downloads**: the job first reads the track list with `yt-dlp --flat-playlist -J`,
  then calls `yt2audio -1 -q <video-url> <job temp dir> <format>` **once per track**.
  That gives per-track progress and failures, lets the duration limit skip long tracks
  before downloading, and maps every file to its video ID. A track counts as done only
  if yt2audio printed a path that exists, never from the exit code alone. Files are then
  moved into the library as `<video_id>.<ext>` (`<video_id>-alac.m4a` for ALAC, so it
  doesn't collide with m4a); titles are used only for download filenames and zip entry
  names (sanitized, collisions numbered).
- **Edits and previews**: `audiocrop -q -f -s … -e … -g … [-n -t …] --fade-in … --fade-out …
  [-p 15] -- <in> <out>`; edits are saved as `<video_id>-edit-<n>.<ext>`. The backend
  validates the same ranges first and shows audiocrop's stderr message on failure.
- All subprocesses get argument arrays (no shell) and their own process group, so a
  shutdown stops the whole tree. URLs are validated (youtube.com, music.youtube.com,
  youtu.be only) and rebuilt from the video/playlist ID before reaching yt-dlp.

### Job queue: in-process worker

Downloads run in parallel, up to `YTL_PARALLEL_DOWNLOADS` (default 3) songs at once:
several jobs can run side by side, and a playlist job downloads several tracks at once,
but one shared limit caps the total number of yt2audio/audiocrop processes, so the LXC
is never running more than that many downloads or edits. Jobs start in submission
order. The same song in the same format is never downloaded twice at once (the second
job waits and then links to it as a duplicate). Editor previews have their own lane,
so a 15-second preview is not stuck behind downloads. Raising the limit much above 3
to 5 makes YouTube throttling (HTTP 429) more likely. Jobs are stored in SQLite, so the queue survives restarts (interrupted
jobs are re-queued once; already downloaded tracks then show as duplicates). This is
simpler than a separate worker service (one unit, no IPC) and robust enough for one
LXC; the cleanup timer is the only separate process.

## Auto-recycle (cleanup timer)

`yt-library-cleanup.timer` runs `yt-library cleanup` every 15 minutes
(`OnCalendar=*:0/15`, `Persistent=true`, so missed runs happen after a reboot). It:

- deletes expired songs (file and row);
- deletes rows whose file is missing and files with no row (after a 10-minute grace);
- removes partial downloads (temp dirs of jobs that aren't running) and stale previews;
- marks jobs with no progress for `YTL_JOB_TIMEOUT` as failed;
- forgets finished jobs after 7 days;
- logs everything it deleted to the journal.

It is safe to run while the app runs and repeatedly: file deletions happen inside
the same SQLite write transaction as the row change, and the worker moves files into
the library inside its own write transaction. Run it by hand with
`sudo yt-library cleanup`.

The storage quota never deletes other people's songs early: a job that would
exceed it (counting songs plus what queued jobs will add) is rejected with a clear
message, and a track that would overflow while running fails with "storage quota is full".

## Security

- No shell anywhere; argument arrays only. Only YouTube URLs are accepted.
- Limits: tracks per job, track duration, queued jobs, storage quota, per-IP rate
  limit on job submission (`X-Forwarded-For` only from `YTL_TRUSTED_PROXIES`).
- State-changing requests must carry a same-origin `Origin` (or `Referer`) header;
  cross-origin POST/DELETE requests get 403.
- Files are served by database ID only, never by a user-supplied path.
- The service runs as the unprivileged `yt-library` user with systemd sandboxing
  (`ProtectSystem=strict`, write access only to the library and database dirs).

## Updating

```sh
cd /opt/yt2audio-src
sudo ./update.sh                 # git pull, re-run install.sh, then upgrade yt-dlp to latest
sudo ./update.sh --keep-pinned   # same, but keep yt-dlp at deploy/yt-dlp.version
sudo ./update.sh --yt-dlp-only   # only upgrade yt-dlp to the latest release
sudo ./update.sh --yt-dlp 2026.8.19   # install an exact yt-dlp version
```

YouTube changes often, so when downloads start failing, `update.sh --yt-dlp-only` is
usually the fix. To make a newer version the pin, update `deploy/yt-dlp.version`.
Note that `install.sh` alone reinstalls the pinned version.

## Development

```sh
pip install -r app/requirements.txt pytest httpx
PYTHONPATH=app pytest tests       # uses a fake yt-dlp in tests/fakebin; needs ffmpeg
```

Run locally: `PYTHONPATH=app YTL_LIBRARY_DIR=./music YTL_DB_PATH=./library.db python3 -m yt_library.cli serve`.

## Assumptions and open decisions

- **Per-track calls to yt2audio** instead of one call per playlist (see above). The
  script is unchanged for command-line use.
- **ALAC files are named `<video_id>-alac.m4a`**, a small deviation from `<video_id>.<ext>`,
  because m4a and alac both use `.m4a`.
- **Parallel downloads** (changed from the original one-job-at-a-time spec at your
  request): default 3, configurable; saved edits share the same limit, previews have
  their own lane.
- **Duplicates** are matched on (video ID, format) among original downloads; edits are
  never treated as duplicates.
- **Extend** sets expiry to `max(current, now + chosen lifetime)`, capped at
  `now + YTL_MAX_LIFETIME`; it never shortens a song's life.
- **Quota estimate** for downloads uses the durations from yt-dlp and a per-format
  bitrate (unknown durations count as 10 minutes); edits reserve the source size.
- **Playlists over the track limit are rejected**, not truncated.
- **Restarts** re-queue interrupted jobs once; previews in progress fail.
- **Origin check** requires an `Origin`/`Referer` header on POST/DELETE, so the API is
  meant for the browser UI; use the scripts directly on the command line.
- **deno** is installed because current yt-dlp needs a JS runtime for YouTube.
- **Not tested against real YouTube** during development (the build sandbox had no
  YouTube access): the tests use a fake yt-dlp with the real scripts and ffmpeg.
  The first real download on the LXC is the remaining check.
- **Open:** cookies for age-restricted videos are not supported in the UI (yt-dlp's
  own config file in `/var/lib/yt-library/.config/yt-dlp/config` could add them);
  there is no per-user attribution of who added or deleted what beyond the journal log.
