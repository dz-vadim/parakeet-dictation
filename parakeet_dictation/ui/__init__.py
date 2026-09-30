"""GTK-facing modules.  gi.require_version is called once, here; every module
under the package imports from gi.repository without repeating it."""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
