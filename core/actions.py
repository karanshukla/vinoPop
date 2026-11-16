"""Built-in actions used by the default plugins."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import urllib.parse

from core.classifier import _normalize as _strip_invisible
from core.classifier import strip_shell_prompts
from core.notify import notify as notify
from core.notify import notify_argv as notify_argv

_local = threading.local()
SELECTION_OWNER_SETTLE_S = 0.05
SNIPPET_PREVIEW_CHARS = 160
DEFAULT_SEARCH_ENGINE = "google"


def force_copy_active() -> bool:
    return bool(getattr(_local, "force_copy", False))


class force_copy_mode:
    """Context manager for a Shift-click on a transform action: while
    active, on this thread only, replace_selection leaves the result on
    the clipboard and doesn't paste it (pinned by
    `tests/test_core_actions.py::test_replace_selection_pastes_over_the_selection`,
    `tests/test_core_actions.py::test_force_copy_mode_leaves_the_result_on_the_clipboard_only`)."""

    def __enter__(self) -> None:
        _local.force_copy = True

    def __exit__(self, *_exc) -> None:
        _local.force_copy = False


def _snippet_preview(text: str) -> str:
    preview = text[:SNIPPET_PREVIEW_CHARS].replace("\n", "  ")
    return preview + "..." if len(text) > SNIPPET_PREVIEW_CHARS else preview


def pin_as_snippet(text: str) -> None:
    """Open a dialog that saves the selection as a snippet through the
    Clipboard history plugin, or say the plugin is needed."""
    import sys

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    ch = sys.modules.get("vinopop_user_clipboard_history") or sys.modules.get("clipboard_history")
    if ch is None or not hasattr(ch, "_create_snippet"):
        notify(
            "Snippets unavailable",
            "Snippets need the Clipboard history plugin enabled.",
            "dialog-warning",
        )
        return

    def _open():
        dlg = Adw.AlertDialog(heading="Pin as snippet")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        # No set_content_width(): it segfaults on AlertDialog in libadwaita 1.5.

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        preview = _snippet_preview(text)
        prev_lbl = Gtk.Label(xalign=0)
        prev_lbl.set_markup(f"<small>Saving: <i>{GLib.markup_escape_text(preview)}</i></small>")
        prev_lbl.set_wrap(True)
        prev_lbl.add_css_class("dim-label")
        content.append(prev_lbl)

        name_lbl = Gtk.Label(xalign=0, margin_top=4)
        name_lbl.set_markup("<b>Name</b>")
        content.append(name_lbl)
        name_entry = Gtk.Entry()
        name_lbl.set_mnemonic_widget(name_entry)
        name_entry.set_placeholder_text("Short label so you'll recognise it")
        name_entry.set_activates_default(True)
        content.append(name_entry)

        trig_lbl = Gtk.Label(xalign=0, margin_top=4)
        trig_lbl.set_markup("<b>Trigger</b>  <small>(optional)</small>")
        content.append(trig_lbl)
        trig_entry = Gtk.Entry()
        trig_lbl.set_mnemonic_widget(trig_entry)
        trig_entry.set_placeholder_text("e.g. ;mysnippet")
        trig_entry.set_activates_default(True)
        content.append(trig_entry)

        cat_lbl = Gtk.Label(xalign=0, margin_top=4)
        cat_lbl.set_markup("<b>Category</b>  <small>(optional)</small>")
        content.append(cat_lbl)
        cat_entry = Gtk.Entry()
        cat_lbl.set_mnemonic_widget(cat_entry)
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
        return GLib.SOURCE_REMOVE

    def _on_response(response: str, name: str, trig: str, category: str) -> None:
        """Saved literal: a {shell:...} in someone else's text is never
        expanded (pinned by
        `tests/test_clipboard_store.py::test_pin_as_snippet_saves_the_selection_as_literal`)."""
        if response != "save":
            return
        try:
            ch._create_snippet(name=name, text=text, trigger=trig, category=category, literal=True)
            notify("Snippet saved", name or text[:40], timeout_ms=2000)
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
    """Put new_text on the clipboard and paste it over the selection. A
    read-only widget drops the paste, leaving the result on the clipboard;
    a backend that can't paste says so (pinned by
    `tests/test_core_actions.py::test_without_auto_paste_the_user_is_told_to_paste`)."""
    from platform_backend import get_backend

    backend = get_backend()
    backend.set_clipboard(new_text)
    if force_copy_active():
        print("[actions] force-copy mode - clipboard only, no paste")
        return
    import time as _t

    _t.sleep(SELECTION_OWNER_SETTLE_S)
    backend.paste()
    if not backend.can_paste():
        notify(
            "Copied to clipboard",
            "Auto-paste isn't available here - press Ctrl+V to paste.",
            "edit-paste",
        )


def _desktop_env() -> dict:
    """os.environ with the session's runtime dir, bus and display filled in
    where missing. Without the bus, Firefox can't find its running instance
    and says it is "already running but not responding" (pinned by
    `tests/test_core_actions.py::test_desktop_env_fills_in_the_session_bus`,
    `tests/test_core_actions.py::test_desktop_env_keeps_a_bus_already_set`)."""
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


_WEB_SCHEMES = ("http", "https", "ftp", "mailto")


def open_web_url(url: str) -> bool:
    """xdg-open `url` if its scheme is a web one; otherwise refuse, with a
    notification saying why. True if it was opened. Selected text is
    whatever a page put there, and another scheme goes to whatever app
    claims it (pinned by `tests/test_url_schemes.py::test_web_links_open`,
    `tests/test_url_schemes.py::test_other_schemes_are_refused`)."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _WEB_SCHEMES:
        shown = f"{scheme}:" if scheme else "a link without a scheme"
        print(f"[actions] refused to open {shown} link")
        notify("Link not opened", f"Not opening {shown}: only web links open from here")
        return False
    from platform_backend import get_backend

    get_backend().open_url(url)
    return True


def _open_file_url(url: str) -> None:
    """A file:// URL: open the folder, never the file. xdg-open on the file
    runs its MIME handler, which for an .exe or .desktop file a page got into
    ~/Downloads means running it (pinned by
    `tests/test_url_schemes.py::test_open_in_browser_on_a_file_url_opens_its_folder`,
    `tests/test_url_schemes.py::test_a_file_url_to_a_folder_opens_that_folder`)."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not folder or not os.path.isdir(folder):
        notify("Folder not found", f"No such folder: {folder or path}")
        return
    try:
        subprocess.Popen(["xdg-open", folder], start_new_session=True, env=_desktop_env())
        print(f"[actions] opened folder of a file URL: {folder}")
    except FileNotFoundError:
        print("[actions] xdg-open not available")


def open_url(text: str) -> None:
    """Open a URL selection; one without a scheme gets https:// (pinned by
    `tests/test_url_schemes.py::test_naked_urls_still_get_https`)."""
    url = _strip_invisible(text)
    if url.lower().startswith("file://"):
        _open_file_url(url)
        return
    if not url.lower().startswith(("http://", "https://", "ftp://", "mailto:")):
        url = "https://" + url
    if open_web_url(url):
        print(f"[actions] opened URL: {url}")


SEARCH_QUERY = "{q}"
# General web search only: site-specific searches are recipes (plugins_repo/recipes/).
SEARCH_ENGINES: dict[str, tuple[str, str]] = {
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
    """The URL template of the chosen search engine; Google for an unknown
    engine or a custom URL without {q} (pinned by
    `tests/test_core_actions.py::test_a_custom_search_url_with_the_query_is_used`,
    `tests/test_core_actions.py::test_a_custom_search_url_without_the_query_falls_back_to_google`,
    `tests/test_core_actions.py::test_an_unknown_search_engine_falls_back_to_google`)."""
    try:
        from core.settings import get_settings

        s = get_settings()
        engine = (s.get("search_engine") or DEFAULT_SEARCH_ENGINE).strip().lower()
        if engine == "custom":
            tmpl = (s.get("search_engine_custom_url") or "").strip()
            if SEARCH_QUERY in tmpl:
                return tmpl
        if engine in SEARCH_ENGINES:
            return SEARCH_ENGINES[engine][1]
    except Exception:
        pass
    return SEARCH_ENGINES[DEFAULT_SEARCH_ENGINE][1]


def search_web(text: str) -> None:
    query = urllib.parse.quote_plus(_strip_invisible(text))
    url = _search_template().replace(SEARCH_QUERY, query)
    from platform_backend import get_backend

    get_backend().open_url(url)


_TERMINALS = [
    ("gnome-terminal", ["gnome-terminal", "--", "bash", "-c"]),
    ("konsole", ["konsole", "-e", "bash", "-c"]),
    # Its -e takes one command string; -x takes the rest of argv.
    ("xfce4-terminal", ["xfce4-terminal", "-x", "bash", "-c"]),
    ("alacritty", ["alacritty", "-e", "bash", "-c"]),
    ("kitty", ["kitty", "bash", "-c"]),
    ("x-terminal-emulator", ["x-terminal-emulator", "-e", "bash", "-c"]),
    ("xterm", ["xterm", "-hold", "-e", "bash", "-c"]),
]


def _find_terminal() -> tuple[str, list[str]] | None:
    """Return (binary, argv-prefix) for the first available terminal emulator."""
    for binary, argv in _TERMINALS:
        if shutil.which(binary):
            return binary, argv
    return None


def _terminal_keep_open() -> bool:
    try:
        from core.settings import get_settings

        return bool(get_settings().get("terminal_keep_open", True))
    except Exception:
        return True


def _should_confirm_terminal() -> bool:
    try:
        from core.settings import get_settings

        return bool(get_settings().get("terminal_confirm_run", True))
    except Exception:
        return True


def _spawn_terminal(argv: list[str], log) -> None:
    """Launch a terminal that outlives vinoPop. gnome-terminal and konsole
    open no window unless stdin is /dev/null (the daemon's is a socket,
    which breaks their D-Bus handshake) and env is an explicit copy
    (Python 3.12's posix_spawn path with env=None dropped
    DBUS_SESSION_BUS_ADDRESS and GIO* for them)."""
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
    """Echo the command as if pasted, run it, then optionally keep a shell
    open. Newlines, not ';', so a trailing '# comment' can't swallow
    'exec bash'; a trailing backslash is dropped so it can't either (pinned by
    `tests/test_terminal_wrap.py::test_keep_open_survives_a_trailing_comment`,
    `tests/test_terminal_wrap.py::test_trailing_backslash_does_not_swallow_exec_bash`,
    `tests/test_terminal_wrap.py::test_command_is_echoed_first`)."""
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


def _collapse_blank_lines(cmd: str) -> str:
    """Squeeze each run of blank lines to one and drop them at the ends, so
    hidden newlines can't push a command below the confirm dialog's preview.
    The result is both shown and run (pinned by
    `tests/test_terminal_confirm.py::test_hidden_newlines_cannot_push_a_command_below_the_fold`,
    `tests/test_terminal_confirm.py::test_run_in_terminal_shows_the_collapsed_command`,
    `tests/test_terminal_confirm.py::test_run_in_terminal_without_confirm_runs_the_collapsed_command`)."""
    out: list[str] = []
    for line in cmd.splitlines():
        if line.strip():
            out.append(line)
        elif out and out[-1]:
            out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _count_command_lines(cmd: str) -> int:
    return sum(1 for line in cmd.splitlines() if line.strip())


def _confirm_heading(cmd: str) -> str:
    n = _count_command_lines(cmd)
    return "Run this command?" if n <= 1 else f"Run these {n} lines?"


_EXPLAIN_ONE_LINE = "Press <b>Run</b> to launch it as-is, <b>Edit</b> to tweak it first."
_EXPLAIN_SEVERAL_LINES = (
    "Check every line, then press <b>Run</b> (or Ctrl+Enter). <b>Edit</b> to tweak it first."
)
_EXPLAIN_EDITING = "<b>Editing</b> -- Ctrl+Enter to run, Esc to cancel."


def _enter_confirms(ctrl: bool, editing: bool, multiline: bool) -> bool:
    """(pinned by `tests/test_terminal_confirm.py::test_plain_enter_confirms_only_a_read_only_single_line`,
    `tests/test_terminal_confirm.py::test_ctrl_enter_always_confirms`)"""
    return ctrl or not (editing or multiline)


def _launch_in_terminal(cmd: str, binary: str, prefix: list[str]) -> None:
    import logging

    log = logging.getLogger("vinopop")
    try:
        _spawn_terminal([*prefix, _wrap_for_terminal(cmd)], log)
        log.info("[terminal] launched (%s), %d chars", binary, len(cmd))
        log.debug("[terminal] command: %s", cmd[:120])
    except OSError:
        pass


class _ConfirmRunDialog:
    """Cancel / Edit / Run over a read-only preview of the command; the
    launch happens in the answer, since GTK 4 has no blocking dialog.
    docs/how-it-works.md, "The Run in terminal dialog"."""

    def __init__(self, cmd: str, binary: str, prefix: list[str]):
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gtk

        self._Adw, self._Gdk, self._Gtk = Adw, Gdk, Gtk
        self._binary = binary
        self._prefix = prefix
        self._multiline = _count_command_lines(cmd) > 1
        self._answered = False

        self.window = Adw.Window(title="Run in terminal")
        self.window.set_default_size(580, 260)
        self.window.set_icon_name("vinopop")

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=14,
            margin_bottom=12,
            margin_start=18,
            margin_end=18,
        )
        box.append(self._build_heading(cmd))
        box.append(self._build_explanation())
        self._scroll, self._text_view = self._build_command_view(cmd)
        box.append(self._scroll)
        box.append(self._build_buttons())
        self._connect_keys()

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(box)
        self.window.set_content(toolbar)

    def present(self) -> None:
        self.window.present()
        (self._cancel if self._multiline else self._run).grab_focus()

    def _build_heading(self, cmd: str):
        head = self._Gtk.Label(xalign=0)
        head.set_markup(f"<span size='large' weight='bold'>{_confirm_heading(cmd)}</span>")
        return head

    def _build_explanation(self):
        explain = self._Gtk.Label(xalign=0)
        explain.add_css_class("dim-label")
        explain.set_wrap(True)
        self._explain = explain
        self._set_explanation(_EXPLAIN_SEVERAL_LINES if self._multiline else _EXPLAIN_ONE_LINE)
        return explain

    def _set_explanation(self, markup: str) -> None:
        self._explain.set_markup(f"<span size='small'>{markup}</span>")

    def _build_command_view(self, cmd: str):
        Gtk = self._Gtk
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(96)
        scroll.set_vexpand(True)
        scroll.set_has_frame(False)
        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_monospace(True)
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.add_css_class("lp-cmd-preview")
        text_view.update_property([Gtk.AccessibleProperty.LABEL], ["Command"])
        text_view.get_buffer().set_text(cmd)
        scroll.set_child(text_view)
        return scroll, text_view

    def _build_buttons(self):
        Gtk = self._Gtk
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=4)
        row.set_halign(Gtk.Align.END)

        self._cancel = Gtk.Button(label="Cancel")
        self._cancel.connect("clicked", lambda *_: self._answer(False))
        row.append(self._cancel)

        self._edit = Gtk.Button(label="Edit")
        self._edit.set_tooltip_text("Unlock the command for editing")
        self._edit.connect("clicked", lambda *_: self._start_editing())
        row.append(self._edit)

        self._run = Gtk.Button(label="Run")
        self._run.add_css_class("suggested-action")
        self._run.connect("clicked", lambda *_: self._answer(True))
        row.append(self._run)
        return row

    def _connect_keys(self) -> None:
        Gtk = self._Gtk
        # Capture phase, or the text view takes Ctrl+Enter as a newline (pinned by
        # `tests/test_terminal_confirm_dialog.py::test_ctrl_enter_in_the_command_field_runs_before_it_becomes_a_newline`).
        text_keys = Gtk.EventControllerKey()
        text_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        text_keys.connect("key-pressed", self._on_text_view_key)
        self._text_view.add_controller(text_keys)

        window_keys = Gtk.EventControllerKey()
        window_keys.connect("key-pressed", self._on_window_key)
        self.window.add_controller(window_keys)
        self.window.connect("close-request", self._on_close_request)

    def _is_enter(self, keyval: int) -> bool:
        return keyval in (self._Gdk.KEY_Return, self._Gdk.KEY_KP_Enter)

    def _ctrl_held(self, state) -> bool:
        return bool(state & self._Gdk.ModifierType.CONTROL_MASK)

    def _on_text_view_key(self, _controller, keyval, _code, state) -> bool:
        if self._is_enter(keyval) and self._ctrl_held(state):
            self._answer(True)
            return True
        return False

    def _on_window_key(self, _controller, keyval, _code, state) -> bool:
        if keyval == self._Gdk.KEY_Escape:
            self._answer(False)
            return True
        if self._is_enter(keyval) and _enter_confirms(
            self._ctrl_held(state), self._text_view.get_editable(), self._multiline
        ):
            self._answer(True)
            return True
        return False

    def _on_close_request(self, *_args) -> bool:
        self._answer(False)
        return False

    def _start_editing(self) -> None:
        text_view = self._text_view
        text_view.set_editable(True)
        text_view.set_cursor_visible(True)
        text_view.remove_css_class("lp-cmd-preview")
        text_view.add_css_class("lp-cmd-edit")
        self._scroll.set_has_frame(True)
        text_view.grab_focus()
        buf = text_view.get_buffer()
        buf.select_range(buf.get_start_iter(), buf.get_end_iter())
        self._edit.set_sensitive(False)
        self._edit.set_label("Editing…")
        self._set_explanation(_EXPLAIN_EDITING)

    def _command_text(self) -> str:
        buf = self._text_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()

    def _answer(self, run: bool) -> None:
        if self._answered:
            return
        self._answered = True
        import logging

        log = logging.getLogger("vinopop")
        if not run:
            log.info("[terminal] cancelled")
        elif edited := self._command_text():
            _launch_in_terminal(edited, self._binary, self._prefix)
        else:
            log.info("[terminal] OK pressed but command was empty -- skipping")
        self.window.close()


def _confirm_run_then_launch(cmd: str, binary: str, prefix: list[str]) -> bool:
    from gi.repository import GLib

    _ConfirmRunDialog(cmd, binary, prefix).present()
    return GLib.SOURCE_REMOVE


def run_in_terminal(text: str) -> None:
    """Run a command selection in a terminal, without its prompts and output
    lines (pinned by `tests/test_terminal_wrap.py::test_prompted_selection_runs_without_the_prompt`),
    after the confirm dialog on the GTK thread unless terminal_confirm_run is off."""
    cmd = _collapse_blank_lines(strip_shell_prompts(_strip_invisible(text)))
    found = _find_terminal()
    if not found:
        print("[actions] no terminal emulator found")
        return
    binary, prefix = found

    if _should_confirm_terminal():
        from gi.repository import GLib

        GLib.idle_add(_confirm_run_then_launch, cmd, binary, prefix)
        return

    _launch_in_terminal(cmd, binary, prefix)


_LAUNCHER_EXTENSIONS = frozenset(
    {".desktop", ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".appimage", ".jar", ".run"}
)


def _is_launcher(path: str) -> bool:
    """True for a regular file xdg-open would run rather than show: one
    with the execute bit, or a launcher extension (.desktop, .exe...)."""
    if not os.path.isfile(path):
        return False
    if os.path.splitext(path)[1].lower() in _LAUNCHER_EXTENSIONS:
        return True
    return os.access(path, os.X_OK)


def open_path(text: str) -> None:
    """Open a selected path with xdg-open. A program or launcher opens its
    folder instead, as a file:// link does: the selection is whatever a page
    put there, and xdg-open on a .desktop file or executable runs it (pinned by
    `tests/test_open_path.py::test_a_launcher_opens_its_folder`,
    `tests/test_open_path.py::test_an_executable_file_opens_its_folder`,
    `tests/test_open_path.py::test_a_plain_file_opens_itself`)."""

    raw = _strip_invisible(text)
    path = os.path.expanduser(raw)
    if _is_launcher(path):
        folder = os.path.dirname(os.path.abspath(path))
        notify("Opened its folder", f"{os.path.basename(path)} is a program, so it wasn't run")
        print(f"[actions] {path} is runnable; opening its folder instead")
        path = folder
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
