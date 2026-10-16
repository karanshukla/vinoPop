#!/usr/bin/env python3
"""Tester at popup-vinduet vises korrekt uten å trenge X11-watcher."""

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

sys.path.insert(0, ".")
import gtk4_app  # noqa: E402
import plugin_loader  # noqa: E402
import theme  # noqa: E402
from classifier import ContentType  # noqa: E402
from popup import PopupWindow  # noqa: E402


def main():
    gtk4_app.init()
    theme.install_premium_theme("dark")
    plugin_loader.load_all()
    popup = PopupWindow()

    test_cases = [
        ("sudo apt update && sudo apt upgrade -y", ContentType.COMMAND, 960, 540),
        ("https://github.com/torvalds/linux", ContentType.URL, 960, 540),
        ("dette er vanlig tekst for testing", ContentType.PLAIN_TEXT, 960, 540),
    ]

    index = [0]

    def show_next():
        if index[0] >= len(test_cases):
            print("Alle tester vist - avslutter")
            gtk4_app.quit()
            return False
        text, ctype, x, y = test_cases[index[0]]
        print(f"Viser: [{ctype.value}] {text[:50]}")
        popup.show_for(text, x, y, ctype)
        index[0] += 1
        return False

    # Vis første test etter 500ms, deretter neste ved klikk utenfor
    GLib.timeout_add(500, show_next)

    def on_hide(*_):
        GLib.timeout_add(400, show_next)

    popup.win.connect("hide", on_hide)

    gtk4_app.run()


if __name__ == "__main__":
    main()
