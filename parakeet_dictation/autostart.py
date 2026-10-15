"""Start at login: ~/.config/autostart/parakeet-dictation.desktop."""

import os
from pathlib import Path

from .config import APP_DIR, APP_ID, APP_NAME


def desktop_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / "autostart" / f"{APP_ID}.desktop"


def exec_line() -> str:
    start = APP_DIR / "start.sh"
    return str(start) if start.is_file() else APP_ID


def is_enabled() -> bool:
    path = desktop_path()
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text


def set_enabled(on: bool) -> None:
    path = desktop_path()
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Offline push-to-talk dictation\n"
        f"Exec={exec_line()}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n")
