#!/usr/bin/env bash
# Update yt-library and/or yt-dlp. Run as root from the repo checkout.
#
#   sudo ./update.sh                  git pull --ff-only (when this is a clean git
#                                     checkout), re-run install.sh, then upgrade
#                                     yt-dlp to the latest release.
#   sudo ./update.sh --keep-pinned    same, but leave yt-dlp at the version pinned
#                                     in deploy/yt-dlp.version.
#   sudo ./update.sh --yt-dlp-only    only upgrade yt-dlp (in /opt/yt-dlp) to the
#                                     latest release.
#   sudo ./update.sh --yt-dlp VER     only install yt-dlp version VER exactly.
#
# Why upgrade past the pin by default: YouTube changes often and old yt-dlp
# releases stop working; the pin is the known-good baseline install.sh uses.
# Running install.sh on its own resets yt-dlp to the pinned version. To make a
# newer version the baseline, write it to deploy/yt-dlp.version.
# The running service does not need a restart after a yt-dlp change (it runs
# yt-dlp as a new process for every job).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YTDLP_VENV=/opt/yt-dlp

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

ytdlp_version() {
    "$YTDLP_VENV/bin/python" -c \
        'import importlib.metadata as m; print(m.version("yt-dlp"))' 2>/dev/null || echo none
}

# install_ytdlp <pip requirement> [extra pip args...]
install_ytdlp() {
    [ -x "$YTDLP_VENV/bin/pip" ] || die "$YTDLP_VENV not found; run ./install.sh first"
    local req="$1" old new
    shift
    old="$(ytdlp_version)"
    "$YTDLP_VENV/bin/pip" install -q --disable-pip-version-check "$@" "$req"
    new="$(ytdlp_version)"
    if [ "$old" = "$new" ]; then
        echo "yt-dlp is at $new (unchanged)."
    else
        echo "yt-dlp upgraded: $old -> $new"
    fi
    echo "Pinned version in deploy/yt-dlp.version: $(tr -d '[:space:]' <"$REPO_DIR/deploy/yt-dlp.version")"
    echo "To keep $new across install.sh runs, write it to deploy/yt-dlp.version."
}

# Run git as the checkout's owner (avoids git's "dubious ownership" refusal
# and root-owned files in a user's checkout).
git_as_owner() {
    local owner
    owner="$(stat -c %U "$REPO_DIR")"
    if [ "$owner" = root ] || [ "$owner" = UNKNOWN ]; then
        git -C "$REPO_DIR" "$@"
    else
        runuser -u "$owner" -- git -C "$REPO_DIR" "$@"
    fi
}

git_pull() {
    if ! command -v git >/dev/null 2>&1 || ! git_as_owner rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Not a git checkout; skipping git pull."
        return 0
    fi
    if [ -n "$(git_as_owner status --porcelain --untracked-files=no)" ]; then
        echo "WARNING: the checkout has local changes; skipping git pull (installing as-is)." >&2
        return 0
    fi
    echo "==> git pull --ff-only"
    git_as_owner pull --ff-only
}

main() {
    local mode=full keep_pinned=0 version=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --yt-dlp-only) mode=ytdlp-latest ;;
            --yt-dlp)
                [ $# -ge 2 ] || die "--yt-dlp needs a version"
                mode=ytdlp-version; version="$2"; shift ;;
            --keep-pinned) keep_pinned=1 ;;
            -h|--help) usage 0 ;;
            *) echo "Unknown option: $1" >&2; usage 1 ;;
        esac
        shift
    done

    [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"

    case "$mode" in
        ytdlp-latest)
            install_ytdlp "yt-dlp[default]" -U ;;
        ytdlp-version)
            install_ytdlp "yt-dlp[default]==$version" ;;
        full)
            git_pull
            # Run the (possibly just updated) installer as a new process.
            bash "$REPO_DIR/install.sh"
            if [ "$keep_pinned" -eq 0 ]; then
                echo
                echo "==> Upgrading yt-dlp to the latest release (use --keep-pinned to skip)"
                install_ytdlp "yt-dlp[default]" -U
            fi ;;
    esac
}

# Everything runs inside main(), which bash parses completely before running,
# so a git pull that rewrites this file cannot confuse the running script.
main "$@"
exit
