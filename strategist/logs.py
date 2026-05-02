"""In-memory log buffer for the strategist API.

Tees stdout + stderr into a bounded deque so the hub UI can show recent
output (uvicorn access logs, FastAPI tracebacks, our own print() calls).
The original streams are preserved untouched, so the launcher's process-
level log capture still sees everything as before.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from collections import deque
from typing import Any, Deque, List

_BUFFER: Deque[str] = deque(maxlen=2000)
_LOCK = threading.Lock()
_INSTALLED = False

# uvicorn access lines look like:
#   127.0.0.1:61320 - "GET /health HTTP/1.1" 200 OK
# Drop the high-frequency UI polls when they succeed — they create a feedback
# loop because the strategist UI reads /logs every 3s, which itself logs.
# Errors (4xx/5xx) and any non-GET still pass through.
_ACCESS_RE = re.compile(
    r'"(?P<method>[A-Z]+)\s+(?P<path>[^\s?]+)[^"]*"\s+(?P<status>\d{3})'
)
_POLL_PATHS = {
    "/health",
    "/logs",
    "/strategy/cuts",
    "/experiment/list",
    "/traces",
}


def _is_noise(line: str) -> bool:
    m = _ACCESS_RE.search(line)
    if not m:
        return False
    if m.group("method") != "GET":
        return False
    status = m.group("status")
    if not status.startswith("2"):  # keep 4xx, 5xx, 3xx
        return False
    path = m.group("path")
    return path in _POLL_PATHS or path.startswith("/traces/")


def add(line: str) -> None:
    line = line.rstrip("\r\n")
    if not line:
        return
    if _is_noise(line):
        return
    with _LOCK:
        _BUFFER.append(line)


def read(last: int = 300) -> List[str]:
    with _LOCK:
        if last <= 0 or last >= len(_BUFFER):
            return list(_BUFFER)
        # Slice the tail without copying the whole deque
        return list(_BUFFER)[-last:]


def clear() -> None:
    with _LOCK:
        _BUFFER.clear()


class _TeeStream:
    """Mirror writes to the original stream and split into lines for the buffer."""
    def __init__(self, original: Any) -> None:
        self._orig = original
        self._partial = ""

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
        except Exception:
            pass
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            add(line)
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return self._orig.isatty()
        except Exception:
            return False

    def fileno(self) -> int:
        return self._orig.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            add(self.format(record))
        except Exception:
            pass


def install() -> None:
    """Idempotently install stdout/stderr tees and a logging handler."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    sys.stdout = _TeeStream(sys.stdout)
    sys.stderr = _TeeStream(sys.stderr)

    handler = _BufferHandler(level=logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # Attach to uvicorn's loggers (which don't propagate to root by default)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", ""):
        logging.getLogger(name).addHandler(handler)
