#!/usr/bin/env bash
# Install or re-deploy yt-library on a Debian 12/13 host (e.g. a Proxmox LXC).
#
#   sudo ./install.sh        (run from the repo checkout)
#
# Safe to re-run: it keeps /etc/yt-library/yt-library.env, reinstalls the app
# from this checkout, reinstalls the pinned yt-dlp (deploy/yt-dlp.version) and
# deno (deploy/deno.version) only when the installed version differs, rewrites
# the systemd units/drop-ins and restarts the service.
#
# Note: running install.sh on its own resets yt-dlp to the pinned version.
# update.sh upgrades it to the latest release afterwards (see update.sh).
#
# Proxmox: the systemd sandboxing in the units needs the container feature
# "nesting=1" (the default for new unprivileged containers).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_USER=yt-library
STATE_DIR=/var/lib/yt-library
CONF_DIR=/etc/yt-library
CONF_FILE=$CONF_DIR/yt-library.env
APP_ROOT=/opt/yt-library
APP_VENV=$APP_ROOT/venv
APP_SRC=$APP_ROOT/src
YTDLP_VENV=/opt/yt-dlp
SYSTEMD_DIR=/etc/systemd/system

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Strip leading zeros from version components so 2026.08.19 == 2026.8.19.
norm_version() { sed -E 's/(^|\.)0+([0-9])/\1\2/g' <<<"$1"; }

check_path() { # name value: absolute, not "/", no whitespace
    case "$2" in
        /|"") die "$1 must not be empty or '/'" ;;
        /*) ;;
        *) die "$1 must be an absolute path (got '$2')" ;;
    esac
    [[ "$2" =~ [[:space:]] ]] && die "$1 must not contain whitespace (got '$2')"
    return 0
}

# ---------------------------------------------------------------- checks
[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
command -v apt-get >/dev/null 2>&1 || die "apt-get not found; this installer supports Debian-based systems only"
command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) is required"
for f in app/pyproject.toml app/requirements.txt bin/yt2audio bin/audiocrop \
         deploy/yt-dlp.version deploy/deno.version deploy/yt-library.env.example \
         deploy/systemd/yt-library.service deploy/systemd/yt-library-cleanup.service \
         deploy/systemd/yt-library-cleanup.timer; do
    [ -e "$REPO_DIR/$f" ] || die "missing $REPO_DIR/$f (run from a complete checkout)"
done

YTDLP_VERSION="$(tr -d '[:space:]' <"$REPO_DIR/deploy/yt-dlp.version")"
DENO_VERSION="$(tr -d '[:space:]' <"$REPO_DIR/deploy/deno.version")"
[ -n "$YTDLP_VERSION" ] || die "deploy/yt-dlp.version is empty"
[ -n "$DENO_VERSION" ] || die "deploy/deno.version is empty"

# ---------------------------------------------------------------- packages
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    ffmpeg python3 python3-venv ca-certificates curl unzip util-linux

# ---------------------------------------------------------------- user
log "Creating system user $SVC_USER"
getent group "$SVC_USER" >/dev/null || groupadd --system "$SVC_USER"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SVC_USER" --home-dir "$STATE_DIR" --no-create-home \
        --shell /usr/sbin/nologin --comment "yt-library service" "$SVC_USER"
fi
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$STATE_DIR"

# ---------------------------------------------------------------- config
log "Configuration ($CONF_FILE)"
install -d -o root -g "$SVC_USER" -m 0750 "$CONF_DIR"
if [ ! -e "$CONF_FILE" ]; then
    install -o root -g "$SVC_USER" -m 0640 "$REPO_DIR/deploy/yt-library.env.example" "$CONF_FILE"
    echo "Created $CONF_FILE from deploy/yt-library.env.example (edit it to taste)."
else
    chown root:"$SVC_USER" "$CONF_FILE"
    chmod 0640 "$CONF_FILE"
    echo "Keeping existing $CONF_FILE."
fi

set -a
# shellcheck disable=SC1090
. "$CONF_FILE"
set +a
LIBRARY_DIR="${YTL_LIBRARY_DIR:-/srv/music}"
DB_PATH="${YTL_DB_PATH:-$STATE_DIR/library.db}"
ONCALENDAR="${YTL_CLEANUP_ONCALENDAR:-*:0/15}"
LISTEN_HOST="${YTL_HOST:-127.0.0.1}"
LISTEN_PORT="${YTL_PORT:-8080}"
check_path YTL_LIBRARY_DIR "$LIBRARY_DIR"
check_path YTL_DB_PATH "$DB_PATH"
LIBRARY_DIR="${LIBRARY_DIR%/}"
DB_DIR="$(dirname "$DB_PATH")"
check_path "directory of YTL_DB_PATH" "$DB_DIR"
if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze calendar "$ONCALENDAR" >/dev/null 2>&1 \
        || die "YTL_CLEANUP_ONCALENDAR='$ONCALENDAR' is not a valid systemd OnCalendar expression"
fi

# ---------------------------------------------------------------- directories
log "Directories: library $LIBRARY_DIR, database dir $DB_DIR"
for d in "$LIBRARY_DIR" "$DB_DIR"; do
    install -d -m 0750 "$d"
    if ! chown "$SVC_USER:$SVC_USER" "$d" || ! chmod 0750 "$d"; then
        # e.g. a host bind mount in an unprivileged LXC with a foreign uid mapping
        warn "could not set owner/mode on $d; make sure $SVC_USER can write to it"
    fi
done

# ---------------------------------------------------------------- yt-dlp
log "yt-dlp $YTDLP_VERSION (venv $YTDLP_VENV)"
if [ ! -x "$YTDLP_VENV/bin/python" ] || ! "$YTDLP_VENV/bin/python" -c '' 2>/dev/null; then
    rm -rf "$YTDLP_VENV"
    python3 -m venv "$YTDLP_VENV"
fi
ytdlp_installed="$("$YTDLP_VENV/bin/python" -c \
    'import importlib.metadata as m; print(m.version("yt-dlp"))' 2>/dev/null || true)"
if [ "$(norm_version "$ytdlp_installed")" != "$(norm_version "$YTDLP_VERSION")" ]; then
    echo "Installing yt-dlp $YTDLP_VERSION (installed: ${ytdlp_installed:-none})"
    "$YTDLP_VENV/bin/pip" install -q --disable-pip-version-check \
        "yt-dlp[default]==$YTDLP_VERSION"
else
    echo "yt-dlp $ytdlp_installed already installed."
fi
ln -sfn "$YTDLP_VENV/bin/yt-dlp" /usr/local/bin/yt-dlp

# ---------------------------------------------------------------- deno
log "deno $DENO_VERSION (JavaScript runtime used by yt-dlp for YouTube)"
case "$(uname -m)" in
    x86_64|amd64) deno_arch=x86_64 ;;
    aarch64|arm64) deno_arch=aarch64 ;;
    *) die "unsupported architecture for deno: $(uname -m)" ;;
esac
deno_installed=""
if [ -x /usr/local/bin/deno ]; then
    deno_installed="$(/usr/local/bin/deno --version 2>/dev/null | awk 'NR==1 {print $2}' || true)"
fi
if [ "$deno_installed" != "$DENO_VERSION" ]; then
    echo "Installing deno $DENO_VERSION (installed: ${deno_installed:-none})"
    deno_zip="deno-$deno_arch-unknown-linux-gnu.zip"
    deno_url="https://github.com/denoland/deno/releases/download/v$DENO_VERSION/$deno_zip"
    deno_tmp="$(mktemp -d)"
    trap 'rm -rf "$deno_tmp"' EXIT
    curl -fsSL --retry 3 -o "$deno_tmp/$deno_zip" "$deno_url"
    # Integrity check against the checksum published with the release.
    if curl -fsSL --retry 3 -o "$deno_tmp/$deno_zip.sha256sum" "$deno_url.sha256sum"; then
        (cd "$deno_tmp" && sha256sum -c --quiet "$deno_zip.sha256sum") \
            || die "deno checksum mismatch"
    else
        warn "no checksum file published for deno $DENO_VERSION; skipping verification"
    fi
    unzip -q -o "$deno_tmp/$deno_zip" deno -d "$deno_tmp"
    install -m 0755 "$deno_tmp/deno" /usr/local/bin/deno
    rm -rf "$deno_tmp"
    trap - EXIT
else
    echo "deno $deno_installed already installed."
fi

# ---------------------------------------------------------------- scripts
log "Installing yt2audio and audiocrop to /usr/local/bin"
install -m 0755 "$REPO_DIR/bin/yt2audio" /usr/local/bin/yt2audio
install -m 0755 "$REPO_DIR/bin/audiocrop" /usr/local/bin/audiocrop

# ---------------------------------------------------------------- app
log "Installing the yt-library app to $APP_ROOT"
install -d -m 0755 "$APP_ROOT"
src_new="$(mktemp -d "$APP_ROOT/src.new.XXXXXX")"
trap 'rm -rf "$src_new"' EXIT
cp -a "$REPO_DIR/app/." "$src_new/"
find "$src_new" \( -name __pycache__ -o -name '*.egg-info' \
    -o -name .venv -o -name venv -o -name .pytest_cache \) -prune -exec rm -rf {} +
chown -R root:root "$src_new"
chmod -R u=rwX,go=rX "$src_new"
chmod 0755 "$src_new"
rm -rf "$APP_SRC.old"
[ -e "$APP_SRC" ] && mv "$APP_SRC" "$APP_SRC.old"
mv "$src_new" "$APP_SRC"
trap - EXIT
rm -rf "$APP_SRC.old"

# Recreate the venv if missing or broken (e.g. after a Debian release upgrade).
if [ ! -x "$APP_VENV/bin/python" ] || ! "$APP_VENV/bin/python" -c '' 2>/dev/null; then
    rm -rf "$APP_VENV"
    python3 -m venv "$APP_VENV"
fi
"$APP_VENV/bin/pip" install -q --disable-pip-version-check -r "$APP_SRC/requirements.txt"
"$APP_VENV/bin/pip" install -q --disable-pip-version-check --no-deps --force-reinstall "$APP_SRC"
[ -x "$APP_VENV/bin/yt-library" ] || die "$APP_VENV/bin/yt-library was not installed (check app/pyproject.toml [project.scripts])"

# CLI wrapper: loads the config and drops to the service user when run as
# root, so the SQLite DB / WAL files never become root-owned.
cat >/usr/local/bin/yt-library.new <<'EOF'
#!/bin/sh
# Installed by yt-library install.sh; do not edit (it is overwritten).
set -e
if [ "$(id -u)" -eq 0 ]; then
    exec runuser -u yt-library -- "$0" "$@"
fi
CONF=/etc/yt-library/yt-library.env
if [ -r "$CONF" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$CONF"
    set +a
else
    echo "yt-library: cannot read $CONF (run as root or as the yt-library user)" >&2
    exit 1
fi
export HOME=/var/lib/yt-library
cd /var/lib/yt-library
exec /opt/yt-library/venv/bin/yt-library "$@"
EOF
chmod 0755 /usr/local/bin/yt-library.new
mv -f /usr/local/bin/yt-library.new /usr/local/bin/yt-library

# ---------------------------------------------------------------- systemd
log "Installing systemd units"
for unit in yt-library.service yt-library-cleanup.service yt-library-cleanup.timer; do
    install -m 0644 "$REPO_DIR/deploy/systemd/$unit" "$SYSTEMD_DIR/$unit"
done

rw_paths="$LIBRARY_DIR"
[ "$DB_DIR" != "$STATE_DIR" ] && [ "$DB_DIR" != "$LIBRARY_DIR" ] && rw_paths="$rw_paths $DB_DIR"
for svc in yt-library.service yt-library-cleanup.service; do
    install -d -m 0755 "$SYSTEMD_DIR/$svc.d"
    cat >"$SYSTEMD_DIR/$svc.d/paths.conf" <<EOF
# Written by install.sh from $CONF_FILE; re-run install.sh after changing paths.
[Service]
ReadWritePaths=$rw_paths
EOF
done
install -d -m 0755 "$SYSTEMD_DIR/yt-library-cleanup.timer.d"
cat >"$SYSTEMD_DIR/yt-library-cleanup.timer.d/schedule.conf" <<EOF
# Written by install.sh from YTL_CLEANUP_ONCALENDAR in $CONF_FILE.
[Timer]
OnCalendar=
OnCalendar=$ONCALENDAR
EOF

was_active=0
systemctl is-active --quiet yt-library.service && was_active=1
systemctl daemon-reload
systemctl enable --now yt-library.service
systemctl enable yt-library-cleanup.timer
systemctl restart yt-library-cleanup.timer   # picks up a changed schedule
if [ "$was_active" -eq 1 ]; then
    log "Restarting yt-library to deploy the new version"
    systemctl restart yt-library.service
fi

# ---------------------------------------------------------------- health check
probe_host="$LISTEN_HOST"
case "$probe_host" in 0.0.0.0|"") probe_host=127.0.0.1 ;; ::|"[::]") probe_host="[::1]" ;; esac
case "$probe_host" in *:*) [[ "$probe_host" == \[* ]] || probe_host="[$probe_host]" ;; esac
url="http://$probe_host:$LISTEN_PORT"
healthy=0
for _ in $(seq 1 20); do
    if curl -fs -o /dev/null --max-time 2 "$url/api/config"; then healthy=1; break; fi
    sleep 1
done

# ---------------------------------------------------------------- summary
log "Done"
if [ "$healthy" -eq 1 ]; then
    echo "yt-library is running and listening on $url"
else
    warn "yt-library did not answer on $url/api/config yet; check the logs below"
    systemctl --no-pager --lines=0 status yt-library.service || true
fi
cat <<EOF

  Config:        $CONF_FILE   (then: systemctl restart yt-library)
  Library:       $LIBRARY_DIR
  Database:      $DB_PATH
  yt-dlp:        $(/usr/local/bin/yt-dlp --version 2>/dev/null || echo '?')   deno: $(/usr/local/bin/deno --version 2>/dev/null | awk 'NR==1 {print $2}')
  Cleanup timer: OnCalendar=$ONCALENDAR  (systemctl list-timers yt-library-cleanup.timer)

  Logs:          journalctl -u yt-library -f
                 journalctl -u yt-library-cleanup
  CLI:           yt-library cleanup   (runs as the $SVC_USER user)

  Put Caddy in front of it: see deploy/Caddyfile.example (YTL_HOST / YTL_TRUSTED_PROXIES).
EOF
