"""System tray indicator."""

from gi.repository import AyatanaAppIndicator3, Gtk

from ..config import APP_ID, APP_NAME, AppConfig
from ..controller import DictationController
from ..hotkeys import HotkeyManager
from ..models import _is_model_downloaded
from .windows import MainWindow, SettingsDialog

# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

class TrayIcon:
    def __init__(self, controller: DictationController, hotkey_mgr: HotkeyManager,
                 main_window: MainWindow):
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        self._main_window = main_window

        self._indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID,
            "audio-input-microphone-muted",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self._indicator.set_title(APP_NAME)

        self._build_menu()
        controller.set_status_callback(self._on_status_update)

    def _build_menu(self):
        menu = Gtk.Menu()
        cfg = self._controller.config

        show_window_item = Gtk.MenuItem(label="Show Window")
        show_window_item.connect("activate", lambda _: (self._main_window.show_all(), self._main_window.present()))
        menu.append(show_window_item)

        menu.append(Gtk.SeparatorMenuItem())

        self._toggle_item = Gtk.MenuItem(label=f"Start Dictation ({cfg.hotkey_toggle})")
        self._toggle_item.connect("activate", self._on_toggle)
        menu.append(self._toggle_item)

        self._pause_item = Gtk.MenuItem(label=f"Pause ({cfg.hotkey_pause})")
        self._pause_item.connect("activate", lambda _: self._controller.pause())
        self._pause_item.set_sensitive(False)
        menu.append(self._pause_item)

        self._status_item = Gtk.MenuItem(label="Idle")
        self._status_item.set_sensitive(False)
        menu.append(self._status_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Model switcher submenu
        model_menu_item = Gtk.MenuItem(label="Model")
        model_submenu = Gtk.Menu()
        active_profile = cfg.model_profile
        for mid, mdata in self._controller.profiles.items():
            downloaded = _is_model_downloaded(mid, self._controller.profiles)
            label = mdata["name"]
            if mid == active_profile:
                label = f"\u2713 {label}"
            elif not downloaded:
                label = f"  {label} (not downloaded)"
            else:
                label = f"  {label}"
            item = Gtk.MenuItem(label=label)
            if downloaded and mid != active_profile:
                item.connect("activate", self._on_switch_model, mid)
            else:
                item.set_sensitive(False)
            model_submenu.append(item)
        model_menu_item.set_submenu(model_submenu)
        menu.append(model_menu_item)

        menu.append(Gtk.SeparatorMenuItem())

        settings_item = Gtk.MenuItem(label="Settings")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        self._indicator.set_menu(menu)

    def _on_switch_model(self, _item, model_id):
        new_config = self._controller.config
        new_config.model_profile = model_id
        self._controller.apply_config(new_config)
        self._hotkey_mgr.rebuild(new_config)
        self._build_menu()
        self.update_ui()

    def _on_toggle(self, _item=None):
        self._controller.toggle()
        self.update_ui()

    def update_ui(self):
        running = self._controller.is_running
        paused = self._controller.is_paused
        cfg = self._controller.config

        if running:
            if paused:
                self._toggle_item.set_label("Resume Dictation")
                self._indicator.set_icon_full("audio-input-microphone-muted", "Paused")
            else:
                key = cfg.hotkey_toggle if cfg.hotkey_mode == "toggle" else cfg.hotkey_stop
                self._toggle_item.set_label(f"Stop Dictation ({key})")
                self._indicator.set_icon_full("audio-input-microphone", "Listening")
            self._pause_item.set_sensitive(True)
        else:
            key = cfg.hotkey_toggle if cfg.hotkey_mode == "toggle" else cfg.hotkey_start
            self._toggle_item.set_label(f"Start Dictation ({key})")
            self._indicator.set_icon_full("audio-input-microphone-muted", "Idle")
            self._pause_item.set_sensitive(False)
            self._status_item.set_label("Idle")

    def _on_status_update(self, text: str):
        self.update_ui()
        self._main_window.on_status_update(text)
        if text:
            display = text[:60] + "\u2026" if len(text) > 60 else text
            self._status_item.set_label(f"\u25b6 {display}")
        else:
            if self._controller.is_running:
                self._status_item.set_label("Ready")
            else:
                self._status_item.set_label("Idle")

    def _on_settings(self, _item):
        SettingsDialog(
            self._controller.config,
            self._controller.profiles_data,
            on_save=self._apply_settings,
        )

    def _apply_settings(self, new_config: AppConfig):
        self._controller.apply_config(new_config)
        self._controller.notify(self._hotkey_mgr.rebuild(new_config))
        self._build_menu()
        self.update_ui()

    def _on_quit(self, _item):
        self._controller.shutdown()
        Gtk.main_quit()
