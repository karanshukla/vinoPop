"""GTK 4 / libadwaita bootstrap shared by the daemon and every window.

GTK 4 has no Gtk.main(); vinoPop runs a plain GLib main loop instead of a
Gtk.Application, so windows can open and close freely (the daemon has no
"main window") and standalone `python3 -m ui.settings_gui`-style runners keep
working. Target: GTK 4.14 and libadwaita 1.5 (Ubuntu 24.04, Fedora 40).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

_loop: GLib.MainLoop | None = None
_ready = False


def init() -> None:
    """Initialise GTK and libadwaita once (idempotent)."""
    global _ready
    if _ready:
        return
    Gtk.init()
    Adw.init()
    _ready = True


def run() -> None:
    """Run the main loop until quit()."""
    global _loop
    init()
    _loop = GLib.MainLoop()
    _loop.run()


def quit() -> None:
    if _loop is not None and _loop.is_running():
        _loop.quit()


def run_window(win: Gtk.Window) -> None:
    """Show `win` and run until it closes: for `if __name__ == "__main__"`."""
    init()
    win.connect("close-request", lambda *_: (quit(), False)[1])
    win.present()
    run()


def present_dialog(dialog: Adw.Dialog, parent: Gtk.Widget | None, on_closed) -> None:
    """Present an Adw.Dialog over `parent` and call on_closed(dialog) once
    it is gone. libadwaita 1.5 doesn't emit "closed" for a dialog with no
    libadwaita window to attach to (it floats in a window of its own), so
    that window going away counts as closed too."""
    done = {"yes": False}

    def _closed(*_a) -> None:
        if not done["yes"]:
            done["yes"] = True
            on_closed(dialog)

    dialog.connect("closed", _closed)
    dialog.present(parent)
    root = dialog.get_root()
    if isinstance(root, Gtk.Window) and not isinstance(root, (Adw.Window, Adw.ApplicationWindow)):
        root.connect("unmap", _closed)
