"""Render the real settings window pages to PNG (offscreen) for review.

Run:  .venv/bin/python tools/render-settings.py OUTDIR [uk|en]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from parakeet_dictation import i18n  # noqa: E402
from parakeet_dictation.config import AppConfig  # noqa: E402
from parakeet_dictation.models import load_model_profiles  # noqa: E402

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
i18n.setup(sys.argv[2] if len(sys.argv) > 2 else "uk")
from parakeet_dictation.ui.settings import SettingsWindow  # noqa: E402

cfg = AppConfig.load()
win = SettingsWindow(lambda: cfg, load_model_profiles(), lambda c: "", lambda: None)
child = win.get_child()
win.remove(child)
off = Gtk.OffscreenWindow()
off.set_size_request(880, 600)
off.add(child)
off.show_all()
for page in ("general", "recognition", "output", "advanced", "about"):
    win.show_page(page)
    for _ in range(50):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
    off.get_pixbuf().savev(str(out / f"settings-{page}.png"), "png", [], [])
    print(out / f"settings-{page}.png")
