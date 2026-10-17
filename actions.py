"""Built-in actions used by the default plugins."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import urllib.parse

from classifier import _normalize as _strip_invisible
from classifier import strip_shell_prompts

# Thread-local 'force copy only' flag. When set, replace_selection
# writes to the clipboard but skips the Ctrl+V step - the popup uses
# this to honour Shift-click on a transform action: result lands on the
# clipboard for manual paste elsewhere, never overwrites the original
# selection. Mirrors PopClip's Shift-modifier behaviour where Shift
# forces a copy instead of a paste.
_local = threading.local()


def notify(title: str, body: str, icon: str = "vinopop", timeout_ms: int = 3000) -> None:
    """Best-effort desktop notification; a missing notify-send is not an error."""
    try:
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                str(timeout_ms),
                "-i",
                icon,
                title,
                body,
            ],
            check=False,
        )
    except FileNotFoundError:
        pass


def force_copy_active() -> bool:
    return bool(getattr(_local, "force_copy", False))


class force_copy_mode:
    """Context manager: while active, replace_selection skips the paste
    step and leaves the result on the clipboard only. Thread-local so
    concurrent plugin executions don't interfere."""

    def __enter__(self) -> None:
        _local.force_copy = True

    def __exit__(self, *_exc) -> None:
        _local.force_copy = False


def pin_as_snippet(text: str) -> None:
    """One-click "save this selection as a snippet" - opens a small
    create-snippet dialog with the highlighted text pre-filled as the
    body. Delegates the actual save to the clipboard_history plugin
    (looked up via sys.modules so we don't import a user-plugin
    directly). Falls back to a notification if the plugin isn't
    loaded."""
    import sys

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    ch = sys.modules.get("vinopop_user_clipboard_history") or sys.modules.get("clipboard_history")
    if ch is None or not hasattr(ch, "_create_snippet"):
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                "3000",
                "-i",
                "dialog-warning",
                "vinoPop",
                "Snippets need the Clipboard history plugin enabled.",
            ],
            check=False,
        )
        return

    def _open():
        # Adw.AlertDialog (libadwaita 1.5) with the form as its extra child;
        # no parent window, so it opens as its own window. GTK 4 has no
        # blocking dialog.run(): the save happens in the "response" handler.
        dlg = Adw.AlertDialog(heading="Pin as snippet")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        # (No set_content_width(): it segfaults on AlertDialog in libadwaita 1.5;
        # the dialog sizes itself to the form.)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Preview of the selection so the user knows what they're saving.
        preview = text[:160].replace("\n", "  ")
        if len(text) > 160:
            preview += "..."
        prev_lbl = Gtk.Label(xalign=0)
        prev_lbl.set_markup(f"<small>Saving: <i>{GLib.markup_escape_text(preview)}</i></small>")
        prev_lbl.set_wrap(True)
        prev_lbl.add_css_class("dim-label")
        content.append(prev_lbl)

        name_lbl = Gtk.Label(xalign=0, margin_top=4)
        name_lbl.set_markup("<b>Name</b>")
        content.append(name_lbl)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Short label so you'll recognise it")
        name_entry.set_activates_default(True)
        content.append(name_entry)

        trig_lbl = Gtk.Label(xalign=0, margin_top=4)
        trig_lbl.set_markup("<b>Trigger</b>  <small>(optional)</small>")
        content.append(trig_lbl)
        trig_entry = Gtk.Entry()
        trig_entry.set_placeholder_text("e.g. ;mysnippet")
        trig_entry.set_activates_default(True)
        content.append(trig_entry)

        cat_lbl = Gtk.Label(xalign=0, margin_top=4)
        cat_lbl.set_markup("<b>Category</b>  <small>(optional)</small>")
        content.append(cat_lbl)
        cat_entry = Gtk.Entry()
        cat_entry.set_placeholder_text("e.g. Email, Code, Personal")
        cat_entry.set_activates_default(True)
        content.append(cat_entry)

        dlg.set_extra_child(content)
        dlg.connect(
            "response",
            lambda _d, response: _on_response(
                response,
                name_entry.get_text().strip(),
                trig_entry.get_text().strip(),
                cat_entry.get_text().strip(),
            ),
        )
        dlg.present(None)
        name_entry.grab_focus()
        return False

    def _on_response(response: str, name: str, trig: str, category: str) -> None:
        if response != "save":
            return
        try:
            ch._create_snippet(name=name, text=text, trigger=trig, category=category)
            subprocess.run(
                [
                    "notify-send",
                    "--hint=byte:transient:1",
                    "-t",
                    "2000",
                    "-i",
                    "vinopop",
                    "vinoPop",
                    f"Saved snippet: {name or text[:40]}",
                ],
                check=False,
            )
        except Exception as exc:
            print(f"[actions] pin_as_snippet save failed: {exc}")

    GLib.idle_add(_open)


def copy_to_clipboard(text: str) -> None:
    """Copy text to the system clipboard (backend: xclip on X11, wl-copy on
    Wayland/KDE)."""
    from platform_backend import get_backend

    get_backend().set_clipboard(text)
    print(f"[actions] copied {len(text)} chars to clipboard")


def replace_selection(new_text: str) -> None:
    """Put new_text on the clipboard AND paste it over the current
    selection - the in-place transform behaviour PopClip has on macOS.

    Sequence:
      1. write the new text to the clipboard
      2. ~50 ms settle so the selection-owner change propagates
      3. send Ctrl+V (backend), which the focused app interprets as
         'paste over the current selection'

    If the focused widget is read-only the paste is silently discarded,
    but the result is still on the clipboard so the user can paste it
    elsewhere - same fallback as a manual Copy.
    """
    from platform_backend import get_backend

    backend = get_backend()
    backend.set_clipboard(new_text)
    if force_copy_active():
        # Shift-modifier on the popup button: result goes to the
        # clipboard, but we deliberately skip the paste so the original
        # selection stays put.
        print("[actions] force-copy mode - clipboard only, no paste")
        return
    import time as _t

    _t.sleep(0.05)
    backend.paste()
    if not backend.can_paste():
        # The result is on the clipboard but wasn't pasted in place. Tell the
        # user so they aren't left wondering why nothing changed.
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                "3000",
                "-i",
                "edit-paste",
                "Copied to clipboard",
                "Auto-paste isn't available here - press Ctrl+V to paste.",
            ],
            check=False,
        )


def _desktop_env() -> dict:
    """Env that lets xdg-open talk to the running desktop session.

    If vinoPop was launched without inheriting DBUS_SESSION_BUS_ADDRESS
    (some systemd-user / detached-shell scenarios), Firefox's
    "is there an instance running?" probe fails - it can't find the
    real process via DBus, sees the profile lockfile, and pops
    "already running but not responding" every time. Filling in the
    canonical /run/user/$UID values is a cheap safety net.
    """
    env = dict(os.environ)
    uid = os.getuid()
    runtime_dir = f"/run/user/{uid}"
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = runtime_dir
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"
    return env


def open_url(text: str) -> None:
    url = _strip_invisible(text)
    # Naked URLs (no scheme) get https:// so the browser routes them correctly
    if not url.lower().startswith(("http://", "https://", "ftp://", "file://", "mailto:")):
        url = "https://" + url
    from platform_backend import get_backend

    get_backend().open_url(url)
    print(f"[actions] opened URL: {url}")


# General-purpose web search engines. The value is a URL template - '{q}'
# gets replaced with the URL-encoded selection. Add to the dict to extend.
#
# Site-specific destinations (Wikipedia, YouTube, MDN, Stack Overflow, etc.)
# live as their own popup buttons via the recipe system - see
# plugins_repo/recipes/. Keep this dict scoped to "general search".
SEARCH_ENGINES: dict[str, tuple[str, str]] = {
    # key:            (display name,     URL template)
    "google": ("Google", "https://www.google.com/search?q={q}"),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q={q}"),
    "bing": ("Bing", "https://www.bing.com/search?q={q}"),
    "brave": ("Brave Search", "https://search.brave.com/search?q={q}"),
    "startpage": ("Startpage", "https://www.startpage.com/do/search?q={q}"),
    "ecosia": ("Ecosia", "https://www.ecosia.org/search?q={q}"),
    "kagi": ("Kagi", "https://kagi.com/search?q={q}"),
    "qwant": ("Qwant", "https://www.qwant.com/?q={q}"),
    "yandex": ("Yandex", "https://yandex.com/search/?text={q}"),
}


def _search_template() -> str:
    """Return the URL template for the user's chosen search engine.
    Falls back to Google if the configured engine is unknown."""
    try:
        from settings import get_settings

        s = get_settings()
        engine = (s.get("search_engine") or "google").strip().lower()
        if engine == "custom":
            tmpl = (s.get("search_engine_custom_url") or "").strip()
            if "{q}" in tmpl:
                return tmpl
            # Empty/invalid custom URL - fall through to default.
        if engine in SEARCH_ENGINES:
            return SEARCH_ENGINES[engine][1]
    except Exception:
        pass
    return SEARCH_ENGINES["google"][1]


def search_web(text: str) -> None:
    query = urllib.parse.quote_plus(_strip_invisible(text))
    url = _search_template().replace("{q}", query)
    from platform_backend import get_backend

    get_backend().open_url(url)


_TERMINALS = [
    ("gnome-terminal", ["gnome-terminal", "--", "bash", "-c"]),
    ("konsole", ["konsole", "-e", "bash", "-c"]),
    # -e takes ONE command string; -x takes the rest of argv, as the others do
    ("xfce4-terminal", ["xfce4-terminal", "-x", "bash", "-c"]),
    ("alacritty", ["alacritty", "-e", "bash", "-c"]),
    ("kitty", ["kitty", "bash", "-c"]),
    ("x-terminal-emulator", ["x-terminal-emulator", "-e", "bash", "-c"]),
    ("xterm", ["xterm", "-hold", "-e", "bash", "-c"]),  # xterm has -hold
]


def _find_terminal() -> tuple[str, list[str]] | None:
    """Return (binary, argv-prefix) for the first available terminal emulator."""
    for binary, argv in _TERMINALS:
        if shutil.which(binary):
            return binary, argv
    return None


def _terminal_keep_open() -> bool:
    try:
        from settings import get_settings

        return bool(get_settings().get("terminal_keep_open", True))
    except Exception:
        return True


def _should_confirm_terminal() -> bool:
    try:
        from settings import get_settings

        return bool(get_settings().get("terminal_confirm_run", True))
    except Exception:
        return True


def _spawn_terminal(argv: list[str], log) -> None:
    """Launch a terminal emulator with hardening that makes the D-Bus
    flavours (gnome-terminal, konsole) actually open a window:

      * stdin=/dev/null -- the daemon's stdin is a socket, and inheriting
        that into the gnome-terminal CLI confuses its D-Bus handshake.
      * env=os.environ.copy() -- the posix_spawn fast path in Python 3.12
        with env=None has been observed to drop some env vars that the
        terminal's D-Bus client needs (DBUS_SESSION_BUS_ADDRESS, GIO*).
        Explicit copy forces the fork+exec path.
      * start_new_session=True -- terminal outlives vinoPop if we exit.
    """
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as exc:
        log.error("[terminal] launch failed: %s", exc)
        raise


def _wrap_for_terminal(cmd: str, keep_open: bool | None = None) -> str:
    """Wrap a user command so the terminal echoes it first (looks like the
    user pasted it), then runs it, and optionally drops into an interactive
    shell so output stays visible.

    The pieces are joined with newlines, not ';': a command ending in
    '# comment' would otherwise comment out '; exec bash' and the
    terminal closed straight away. A trailing backslash is dropped so it
    can't continue the command onto the 'exec bash' line."""
    if keep_open is None:
        keep_open = _terminal_keep_open()
    cmd = cmd.rstrip()
    if cmd.endswith("\\"):
        cmd = cmd[:-1].rstrip()
    echoed = shlex.quote(f"\033[1;32m$\033[0m {cmd}")
    wrapped = f"printf '%s\\n' {echoed}\n{cmd}"
    if keep_open:
        wrapped += "\nexec bash"
    return wrapped


def _confirm_run_then_launch(cmd: str, binary: str, prefix: list[str]) -> bool:
    """GTK confirmation dialog. Default state is read-only preview --
    Cancel / Edit / Run. Clicking Edit unlocks the command field for
    in-place tweaking. Returns False so GLib.idle_add doesn't re-fire.

    GTK 4 has no blocking dialog.run(), so the dialog is a small modal-less
    Adw.Window and the launch happens in its response callback."""
    import logging

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gtk

    log = logging.getLogger("vinopop")

    dlg = Adw.Window(title="Run in terminal")
    dlg.set_default_size(580, 260)
    dlg.set_icon_name("vinopop")

    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=10,
        margin_top=14,
        margin_bottom=12,
        margin_start=18,
        margin_end=18,
    )

    head = Gtk.Label(xalign=0)
    head.set_markup("<span size='large' weight='bold'>Run this command?</span>")
    box.append(head)

    explain = Gtk.Label(xalign=0)
    explain.add_css_class("dim-label")
    explain.set_markup(
        "<span size='small'>"
        "Press <b>Run</b> to launch it as-is, <b>Edit</b> to tweak it first."
        "</span>"
    )
    explain.set_wrap(True)
    box.append(explain)

    # Command preview/editor. Monospace TextView holds any character
    # (including '&', '<', '>', shell metacharacters) without the
    # Pango-markup ambiguity that the old MessageDialog had.
    #
    # Two visual states, toggled by the Edit button:
    #   .lp-cmd-preview -> read-only, flat on the dialog background,
    #                      looks like a label (the "old preview" look)
    #   .lp-cmd-edit    -> editable, picks up the input-field styling
    #                      (border + slightly lighter bg + caret)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(96)
    scroll.set_vexpand(True)
    # No frame in the read-only state -- the framed look gives the
    # white-box look the user disliked. Re-enabled on entering edit mode.
    scroll.set_has_frame(False)
    text_view = Gtk.TextView()
    text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    text_view.set_monospace(True)
    text_view.set_editable(False)
    text_view.set_cursor_visible(False)
    text_view.add_css_class("lp-cmd-preview")
    text_buf = text_view.get_buffer()
    text_buf.set_text(cmd)
    scroll.set_child(text_view)
    box.append(scroll)

    done = [False]

    def _respond(ok: bool) -> None:
        if done[0]:
            return
        done[0] = True
        if ok:
            start, end = text_buf.get_start_iter(), text_buf.get_end_iter()
            edited = text_buf.get_text(start, end, True).strip()
            if not edited:
                log.info("[terminal] OK pressed but command was empty -- skipping")
            else:
                wrapped = _wrap_for_terminal(edited)
                try:
                    _spawn_terminal([*prefix, wrapped], log)
                    log.info("[terminal] launched (%s): %s", binary, edited[:120])
                except OSError:
                    pass  # already logged inside _spawn_terminal
        else:
            log.info("[terminal] cancelled")
        dlg.close()

    # Button row: Cancel · Edit · Run
    btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=4)
    btn_box.set_halign(Gtk.Align.END)

    cancel = Gtk.Button(label="Cancel")
    cancel.connect("clicked", lambda *_: _respond(False))
    btn_box.append(cancel)

    edit_btn = Gtk.Button(label="Edit")
    edit_btn.set_tooltip_text("Unlock the command for editing")

    def _enter_edit_mode(*_):
        text_view.set_editable(True)
        text_view.set_cursor_visible(True)
        # Swap visual state: drop the flat preview look, pick up the
        # input-field look (border + slight bg + caret).
        text_view.remove_css_class("lp-cmd-preview")
        text_view.add_css_class("lp-cmd-edit")
        scroll.set_has_frame(True)
        text_view.grab_focus()
        # Select all so the user can just start typing to replace, or
        # press End/Arrow to position the caret to tweak.
        text_buf.select_range(text_buf.get_start_iter(), text_buf.get_end_iter())
        # The button has done its job - disable so it visually confirms
        # "you're in edit mode now".
        edit_btn.set_sensitive(False)
        edit_btn.set_label("Editing…")
        explain.set_markup(
            "<span size='small'><b>Editing</b> -- Ctrl+Enter to run, Esc to cancel.</span>"
        )

    edit_btn.connect("clicked", _enter_edit_mode)
    btn_box.append(edit_btn)

    run_btn = Gtk.Button(label="Run")
    run_btn.add_css_class("suggested-action")
    run_btn.connect("clicked", lambda *_: _respond(True))
    btn_box.append(run_btn)

    box.append(btn_box)

    # Keyboard shortcuts. Two separate controllers because key events are
    # dispatched to the focused widget -- if GtkTextView's default handler
    # consumes Enter (to insert a newline), the window-level handler never
    # sees it. So Ctrl+Enter is intercepted ON the TextView in the capture
    # phase, before its default handler runs.
    def _on_textview_key(_c, keyval, _code, state):
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if is_enter and ctrl:
            _respond(True)
            return True  # consume, don't insert a newline
        return False  # let TextView handle plain Enter etc. normally

    tv_keys = Gtk.EventControllerKey()
    tv_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    tv_keys.connect("key-pressed", _on_textview_key)
    text_view.add_controller(tv_keys)

    def _on_dialog_key(_c, keyval, _code, state):
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            _respond(False)
            return True
        if is_enter and (ctrl or not text_view.get_editable()):
            _respond(True)
            return True
        return False

    dlg_keys = Gtk.EventControllerKey()
    dlg_keys.connect("key-pressed", _on_dialog_key)
    dlg.add_controller(dlg_keys)
    # Window-manager close (title-bar X) counts as Cancel.
    dlg.connect("close-request", lambda *_: (_respond(False), False)[1])

    # Adw.Window draws no title bar of its own; a header bar keeps the
    # dialog movable/closable like the GTK 3 Gtk.Dialog was.
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(box)
    dlg.set_content(toolbar)
    dlg.present()
    # Focus the Run button by default so plain Enter confirms the as-is
    # command (the common path). Edit button is one Tab away.
    run_btn.grab_focus()
    return False


def run_in_terminal(text: str) -> None:
    # The classifier counts '$ cmd' / root '# cmd' lines as commands, so
    # strip those prompts (and, in a copied session, the output lines)
    # before bash sees them. The confirm dialog shows the cleaned text.
    cmd = strip_shell_prompts(_strip_invisible(text))
    found = _find_terminal()
    if not found:
        print("[actions] no terminal emulator found")
        return
    binary, prefix = found

    if _should_confirm_terminal():
        # Marshal to the GTK main thread for the modal dialog. Setting
        # terminal_confirm_run=false in settings.json skips this prompt.
        # The dialog rebuilds the wrapped command after the user edits.
        from gi.repository import GLib

        GLib.idle_add(_confirm_run_then_launch, cmd, binary, prefix)
        return

    import logging

    log = logging.getLogger("vinopop")
    wrapped = _wrap_for_terminal(cmd)
    try:
        _spawn_terminal([*prefix, wrapped], log)
        log.info("[terminal] launched (%s): %s", binary, cmd[:120])
    except OSError:
        pass  # already logged


def open_path(text: str) -> None:
    raw = _strip_invisible(text)
    path = os.path.expanduser(raw)
    try:
        subprocess.Popen(
            ["xdg-open", path],
            start_new_session=True,
            env=_desktop_env(),
        )
        print(f"[actions] opened path: {path}")
    except FileNotFoundError:
        print("[actions] xdg-open not available")


def compose_email(text: str) -> None:
    addr = urllib.parse.quote(_strip_invisible(text), safe="@,+")
    try:
        subprocess.Popen(
            ["xdg-open", f"mailto:{addr}"],
            start_new_session=True,
            env=_desktop_env(),
        )
    except FileNotFoundError:
        print("[actions] xdg-open not available")
