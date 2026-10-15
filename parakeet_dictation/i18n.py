"""Interface translations (gettext).  Diagnostics are never translated."""

import gettext as _gettext
import os
from pathlib import Path

DOMAIN = "parakeet-dictation"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
_translation = _gettext.NullTranslations()


def _system_languages():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return [part.split(".")[0].split("_")[0] for part in value.split(":") if part]
    return []


def setup(language: str = "system") -> str:
    """Load the catalogue for `language` ("system", "uk", "en").  Returns
    the language now in effect: "en" whenever no catalogue matches."""
    global _translation
    wanted = _system_languages() if language == "system" else [language]
    for lang in wanted:
        if lang == "en":
            break
        try:
            _translation = _gettext.translation(DOMAIN, LOCALE_DIR, languages=[lang])
            return lang
        except OSError:
            continue
    _translation = _gettext.NullTranslations()
    return "en"


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
