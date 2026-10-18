# vinopop-plugin-version: 1
"""Display the selection in giant text on screen.

Lifted straight from PopClip - useful for reading something to a
colleague across the room, showing a confirmation number from your
screen to a friend on the phone, or just enlarging a tiny menu item
your eyes don't like. Click anywhere or press Esc / Space to dismiss.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from plugin_base import Plugin  # noqa: E402

_CSS = """
window.vinopop-large-type {
    background-color: rgba(10, 12, 20, 0.92);
}

label.vinopop-large-type-label {
    color: #f7f8fa;
    font-size: 96pt;
    font-weight: 600;
    padding: 60px;
}
"""

_css_installed = False


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):  # GTK 4.12+
        provider.load_from_string(_CSS)
    else:
        provider.load_from_data(_CSS.encode(), -1)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
    _css_installed = True


def _show_large(text: str) -> None:
    # GTK calls must hop to the main loop - popup plugin handlers run
    # in worker threads, but Gtk.Window construction needs the main
    # thread to avoid threading errors.
    def _build() -> bool:
        _install_css()
        win = Gtk.Window()
        win.set_title("vinoPop - Large Type")
        win.set_decorated(False)
        win.fullscreen()
        # GTK 4 windows are translucent wherever the CSS background is, so
        # no RGBA visual setup is needed for the dim backdrop.
        win.add_css_class("vinopop-large-type")

        label = Gtk.Label()
        label.set_text(text)
        label.set_wrap(True)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_xalign(0.5)
        label.set_yalign(0.5)
        label.set_selectable(True)
        label.add_css_class("vinopop-large-type-label")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(label)
        win.set_child(scroll)

        def _dismiss(*_):
            win.destroy()
            return True

        keys = Gtk.EventControllerKey()
        # Capture phase: the selectable label takes focus and would see the
        # keys first otherwise.
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect(
            "key-pressed",
            lambda _c, keyval, _code, _state: (
                _dismiss() if keyval in (Gdk.KEY_Escape, Gdk.KEY_space, Gdk.KEY_Return) else False
            ),
        )
        win.add_controller(keys)
        click = Gtk.GestureClick()
        click.set_button(0)  # any button, like button-press-event
        click.connect("pressed", _dismiss)
        win.add_controller(click)
        win.present()
        return False

    GLib.idle_add(_build)


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="large-type",
            icon="zoom-in-symbolic",
            tooltip="Show as large text",
            handler=_show_large,
            content_types=(),  # works for anything
            priority=70,
        )
    )
