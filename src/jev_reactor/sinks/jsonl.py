"""Append-only JSONL sink.

One record is one line, written with a single ``write`` and flushed immediately, so an
interrupted process loses at most the record being written. Readers tolerate a truncated
final line (see ``replay.load_events``).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TextIO

from jev_reactor.models import DecisionEvent


class JsonlSink:
    def __init__(self, path: str | Path, *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._handle: TextIO | None = None

    def _open(self) -> TextIO:
        if self._handle is None:
            self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        return self._handle

    async def emit(self, record: DecisionEvent) -> None:
        line = record.to_jsonl() + "\n"
        with self._lock:
            handle = self._open()
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
