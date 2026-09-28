"""yt-library command line: `serve` and `cleanup`."""

from __future__ import annotations

import argparse
import logging
import sys

from . import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yt-library", description="Shared temporary music library")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web app and job worker")
    sub.add_parser("cleanup", help="delete expired songs, orphans, partial downloads and stale previews")
    args = parser.parse_args(argv)

    # stdout/stderr go to the journal under systemd; no timestamps needed there
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"yt-library: config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "cleanup":
        from . import cleanup
        cleanup.run(cfg)
        return 0

    import uvicorn

    from .main import create_app
    # proxy_headers off: X-Forwarded-For is only honoured for YTL_TRUSTED_PROXIES (see security.py)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=False,
                server_header=False, log_level="info", workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
