"""System tray indicator: renders ui.menu_model, owns the settings window."""

import copy
from pathlib import Path

from gi.repository import AyatanaAppIndicator3, Gtk

from ..audio import list_input_devices
from ..config import APP_ID, AppConfig
from ..models import _is_model_downloaded
from . import menu_model

_ICONS_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
_LEGACY_ICON = {"idle": "audio-input-microphone-muted",
                "listening": "audio-input-microphone",
                "paused": "audio-input-microphone-muted"}


class TrayIcon:
    def __init__(self, controller, hotkey_mgr):
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        self._settings = None
        self._indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID, "audio-input-microphone-muted",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self._custom_icons = (_ICONS_DIR / "parakeet-mic-idle.svg").is_file()
        if self._custom_icons:
            self._indicator.set_icon_theme_path(str(_ICONS_DIR))
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        controller.set_status_callback(lambda _text: self.update_ui())
        self.update_ui()

    # -- state ---------------------------------------------------------------
    def _state(self) -> str:
        if not self._controller.is_running:
            return "idle"
        return "paused" if self._controller.is_paused else "recording"

    def _icon_name(self, state: str) -> str:
        icon_state = {"idle": "idle", "recording": "listening", "paused": "paused"}[state]
        if self._custom_icons:
            return f"parakeet-{self._controller.config.tray_icon_style}-{icon_state}"
        return _LEGACY_ICON[icon_state]

    def update_ui(self):
        state = self._state()
        cfg = self._controller.config
        self._indicator.set_icon_full(self._icon_name(state), state)
        self._indicator.set_title(menu_model.tooltip(state, cfg))
        try:
            devices = [d["name"] for d in list_input_devices()]
        except Exception:
            devices = []
        profiles = self._controller.profiles
        downloaded = {m for m in profiles if _is_model_downloaded(m, profiles)}
        items = menu_model.build_menu(state, cfg, profiles, downloaded, devices)
        self._indicator.set_menu(self._render(items))

    # -- rendering -------------------------------------------------------------
    def _render(self, items) -> Gtk.Menu:
        menu = Gtk.Menu()
        group = None
        for item in items:
            if item.kind == "separator":
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if item.kind == "submenu":
                mi = Gtk.MenuItem(label=item.label)
                mi.set_submenu(self._render(item.children))
            elif item.kind == "radio":
                mi = Gtk.RadioMenuItem.new_with_label_from_widget(group, item.label)
                group = group or mi
                mi.set_active(item.active)
                mi.connect("toggled", lambda w, a=item.action: w.get_active() and self._do(a))
            else:
                mi = Gtk.MenuItem(label=f"{item.label}\t{item.accel}" if item.accel else item.label)
                mi.connect("activate", lambda _w, a=item.action: self._do(a))
            menu.append(mi)
        menu.show_all()
        return menu

    # -- actions ---------------------------------------------------------------
    def _do(self, action: str):
        cfg = self._controller.config
        if action == "toggle":
            self._controller.toggle(hold_off=False)
        elif action in ("pause", "resume"):
            self._controller.pause()
        elif action == "settings":
            self.open_settings("general")
        elif action == "about":
            self.open_settings("about")
        elif action == "download_models":
            self.open_settings("recognition")
        elif action == "quit":
            self._controller.shutdown()
            Gtk.main_quit()
            return
        elif action.startswith("mic:"):
            new = copy.deepcopy(cfg)
            new.audio_device = action[4:]
            if new.audio_device != cfg.audio_device:
                self.apply_settings(new)
            return
        elif action.startswith("model:"):
            new = copy.deepcopy(cfg)
            new.model_profile = action[6:]
            if new.model_profile != cfg.model_profile:
                self.apply_settings(new)
            return
        self.update_ui()

    def open_settings(self, page: str = "general"):
        if self._settings is None:
            from .settings import SettingsWindow
            self._settings = SettingsWindow(lambda: self._controller.config,
                                            self._controller.profiles_data,
                                            on_apply=self.apply_settings,
                                            on_models_changed=self.update_ui)
        elif not self._settings.get_visible():
            # Long-lived window: the config, autostart and devices may have
            # changed (tray menu, KDE shortcuts) since it was last shown.
            self._settings.refresh_from_config()
        self._settings.show_page(page)
        self._settings.present()

    def apply_settings(self, cfg: AppConfig) -> str:
        """The one place a settings change lands (window, menu, KDE)."""
        self._controller.apply_config(cfg)
        problem = self._hotkey_mgr.rebuild(cfg)
        self._controller.notify(problem)
        self.update_ui()
        return problem

    def on_binding_changed(self, key: str, binding: str):
        new = copy.deepcopy(self._controller.config)
        setattr(new, key, binding)
        self.apply_settings(new)
