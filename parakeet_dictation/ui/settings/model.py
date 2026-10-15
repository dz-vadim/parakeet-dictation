"""What the settings window edits — a draft over AppConfig, no GTK."""

import copy

from ...config import AppConfig

PAGE_KEYS = {
    "general": ("hotkey_hold", "hotkey_toggle", "audio_device", "beep_volume",
                "night_mode", "night_start", "night_end", "ui_language"),
    "recognition": ("model_profile", "language", "preview", "filter_fillers"),
    "output": ("insert_mode", "keep_on_clipboard", "overlay", "overlay_position",
               "tray_icon_style"),
    "advanced": ("num_threads", "vad_min_silence", "paste_min_delay_ms",
                 "paste_transport", "terminal_window_classes", "diagnostics"),
    "about": (),
}
_ALL_KEYS = tuple(k for keys in PAGE_KEYS.values() for k in keys)


class SettingsDraft:
    def __init__(self, applied: AppConfig):
        self.reload(applied)

    def reload(self, applied: AppConfig):
        self._snapshot = {k: copy.deepcopy(getattr(applied, k)) for k in _ALL_KEYS}
        self.values = copy.deepcopy(self._snapshot)

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        if key not in self.values:
            raise KeyError(key)
        self.values[key] = value

    @property
    def dirty(self) -> bool:
        return self.values != self._snapshot

    def changed_keys(self):
        return [k for k in _ALL_KEYS if self.values[k] != self._snapshot[k]]

    def reset_page(self, page: str):
        defaults = AppConfig()
        for key in PAGE_KEYS[page]:
            self.values[key] = copy.deepcopy(getattr(defaults, key))

    def commit(self, current: AppConfig) -> AppConfig:
        out = copy.deepcopy(current)
        for key in self.changed_keys():
            setattr(out, key, copy.deepcopy(self.values[key]))
        self.reload(out)
        return out
