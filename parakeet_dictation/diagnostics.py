"""Structured diagnostics log — the DIAG singleton lives here."""

import os
import threading
from datetime import datetime
from pathlib import Path

# Computed here rather than imported: this module is a leaf of the package
# (config and diagnostics import nothing from it), so the data directory is
# spelled out the same way config.py spells it.
APP_ID = "parakeet-dictation"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_ID
DIAG_LOG_FILE = DATA_DIR / "diagnostics.log"
DIAG_LOG_MAX_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Diagnostics — one `key=value` line per event.  Never logs transcript text or
# audio device names; segment sizes and timings only.
# ---------------------------------------------------------------------------

def _diag_scrub(value) -> str:
    """Values must stay single tokens so the line can be split on whitespace."""
    return str(value).replace(" ", "_").replace("\n", "_").replace("=", "-")


class DiagnosticLog:
    """Append-only key=value log, capped by keeping the newest complete lines."""

    def __init__(self, path: Path = DIAG_LOG_FILE, max_bytes: int = DIAG_LOG_MAX_BYTES):
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._enabled = True

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)

    def log(self, event: str, **fields):
        if not self._enabled:
            return
        parts = [f"ts={datetime.now().isoformat(timespec='milliseconds')}",
                 f"event={_diag_scrub(event)}"]
        for key, value in fields.items():
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, float):
                value = f"{value:.1f}"
            parts.append(f"{key}={_diag_scrub(value)}")
        line = " ".join(parts) + "\n"
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                if self._path.stat().st_size > self._max_bytes:
                    self._trim()
        except OSError:
            pass  # diagnostics must never break dictation

    def _trim(self):
        """Rewrite the file with the newest complete lines (half the cap)."""
        keep = max(self._max_bytes // 2, 1)
        with open(self._path, "rb") as fh:
            fh.seek(-keep, os.SEEK_END)
            data = fh.read()
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1:]  # drop the partial line the seek landed in
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(self._path)


DIAG = DiagnosticLog()
