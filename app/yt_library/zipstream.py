"""Stream a zip archive to the client without building it on disk."""

from __future__ import annotations

import os
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

CHUNK = 256 * 1024


class _Sink:
    """Write-only, non-seekable file object that buffers what zipfile writes."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def stream_zip(items: list[tuple[Path, str]]) -> Iterator[bytes]:
    """Yield a zip of (path, archive name) pairs. Audio is already compressed, so entries are stored."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, name in items:
            try:
                f = open(path, "rb")
            except FileNotFoundError:
                continue  # deleted since the request started
            with f:
                st = os.fstat(f.fileno())
                zi = zipfile.ZipInfo(name, date_time=time.localtime(st.st_mtime)[:6])
                zi.compress_type = zipfile.ZIP_STORED
                zi.file_size = st.st_size
                with zf.open(zi, "w", force_zip64=True) as dest:
                    while chunk := f.read(CHUNK):
                        dest.write(chunk)
                        if len(sink.buf) >= CHUNK:
                            yield sink.take()
            yield sink.take()
    yield sink.take()
