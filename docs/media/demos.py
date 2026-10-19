#!/usr/bin/env python3
"""Draws the README's animated demos as SVG: a simulated Plasma desktop.

    python3 docs/media/demos.py   # writes docs/media/popup.svg and clipboard.svg

Nothing here is a screen capture. The bar, the AI bubble and the clipboard
picker follow vinoPop's dark theme; the notes, the summary and the history
entries are made up.
"""

from html import escape
from pathlib import Path

W, H = 960, 540
FONT = "'Noto Sans','Inter','Segoe UI',system-ui,sans-serif"
MONO = "'Noto Sans Mono','DejaVu Sans Mono',ui-monospace,monospace"
CHAR = 9.0  # Advance of one 15 px monospace glyph.
BAR, RIM, INK, MUTED, GREEN = "#0e0e10", "#2a2c30", "#fafafa", "#a0a4ac", "#1f7d42"


class Timeline:
    """One looping clock. Each element gets keyframes that show it from t0 to t1."""

    def __init__(self, length: float):
        self.length = length
        self.frames: list[str] = []

    def pct(self, t: float) -> str:
        return f"{max(0.0, min(100.0, t / self.length * 100)):.3f}%"

    def show(self, t0: float, t1: float, fade: float = 0.0, rise: float = 0.0) -> str:
        """A style attribute that shows the element from t0 to t1, fading over `fade` s."""
        name = f"k{len(self.frames)}"
        off = f"opacity:0;transform:translateY({rise}px)" if rise else "opacity:0"
        on = "opacity:1;transform:translateY(0)" if rise else "opacity:1"
        f = max(fade, 0.001)
        steps = [(0, off), (t0, off), (t0 + f, on), (t1, on), (t1 + f, off), (self.length, off)]
        body = "".join(f"{self.pct(t)}{{{s}}}" for t, s in steps)
        self.frames.append(f"@keyframes {name}{{{body}}}")
        return f'style="animation-name:{name}"'

    def move(self, points: list[tuple[float, float, float]]) -> str:
        """A style attribute that glides the element through (t, x, y) points."""
        name = f"k{len(self.frames)}"
        points = [(0.0, *points[0][1:]), *points, (self.length, *points[-1][1:])]
        body = "".join(f"{self.pct(t)}{{transform:translate({x}px,{y}px)}}" for t, x, y in points)
        self.frames.append(f"@keyframes {name}{{{body}}}")
        return f'style="animation:{name} {self.length}s linear infinite"'

    def css(self) -> str:
        return (
            f"<style>.a{{animation-duration:{self.length}s;animation-iteration-count:infinite;"
            "animation-timing-function:linear;opacity:0}"
            ".caret{animation:blink 1s steps(1) infinite}@keyframes blink{50%{opacity:0}}"
            f"text{{font-family:{FONT}}}.mono{{font-family:{MONO}}}{''.join(self.frames)}</style>"
        )


def text(x, y, s, size=14, fill=INK, weight=400, anchor="start", extra=""):
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-weight="{weight}" '
        f'text-anchor="{anchor}" {extra}>{escape(s)}</text>'
    )


def mono(x, y, s, fill="#dfe1e3"):
    return text(x, y, s, 15, fill, extra='class="mono" xml:space="preserve"')


def desktop() -> str:
    """Wallpaper and a bottom panel."""
    return (
        '<defs><linearGradient id="wall" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#16233a"/><stop offset=".55" stop-color="#26304f"/>'
        '<stop offset="1" stop-color="#3a2a4f"/></linearGradient>'
        '<radialGradient id="glow" cx=".72" cy=".7" r=".5"><stop offset="0" stop-color="#56cc78" stop-opacity=".16"/>'
        '<stop offset="1" stop-color="#56cc78" stop-opacity="0"/></radialGradient></defs>'
        f'<rect width="{W}" height="{H}" fill="url(#wall)"/><rect width="{W}" height="{H}" fill="url(#glow)"/>'
        f'<rect y="{H - 36}" width="{W}" height="36" fill="#1b1e20" fill-opacity=".92"/>'
        + "".join(
            f'<rect x="{14 + i * 40}" y="{H - 28}" width="20" height="20" rx="5" fill="{c}"/>'
            for i, c in enumerate(["#3daee9", "#e8a23a", "#56cc78", "#a0a4ac"])
        )
        + text(W - 16, H - 13, "18:20", 13, "#dfe1e3", anchor="end")
    )


def keycap(tl, t0, t1, label, width) -> str:
    return (
        f'<g class="a" {tl.show(t0, t1, 0.2)}><rect x="24" y="{H - 82}" width="{width}" height="32" rx="6" '
        f'fill="#1b1e20" stroke="#4d5359"/>{text(24 + width / 2, H - 61, label, 14, "#dfe1e3", 600, "middle")}</g>'
    )


def editor(x, y, w, h, title) -> str:
    """A Breeze Dark editor window; its text starts at (x + 30, y + 70)."""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#232629" stroke="#4d5359"/>'
        f'<path d="M{x} {y + 8} a8 8 0 0 1 8 -8 h{w - 16} a8 8 0 0 1 8 8 v26 h-{w} z" fill="#2a2e32"/>'
        + text(x + w / 2, y + 22, title, 13, "#dfe1e3", 600, "middle")
        + "".join(
            f'<circle cx="{x + w - 18 - i * 22}" cy="{y + 17}" r="6" fill="#4d5359"/>'
            for i in range(3)
        )
        + f'<rect x="{x}" y="{y + 34}" width="44" height="{h - 42}" fill="#1f2225"/>'
    )


def cursor(tl, points) -> str:
    """The mouse arrow, its tip at the origin."""
    return (
        f'<g {tl.move(points)}><path d="M0 0v17l4.5-4.2 3.1 7 2.9-1.3-3.1-6.8h6.1z" '
        'fill="#fff" stroke="#111" stroke-width="1.2" stroke-linejoin="round"/></g>'
    )


# Bar glyphs, drawn in an 18 px box at the origin.
GLYPHS = {
    "copy": '<rect x="5" y="1.5" width="11" height="12" rx="2" fill="none" stroke="{c}" stroke-width="1.7"/>'
    '<path d="M3 5v10.5h9" fill="none" stroke="{c}" stroke-width="1.7" stroke-linecap="round"/>',
    "search": '<circle cx="7.5" cy="7.5" r="5.5" fill="none" stroke="{c}" stroke-width="1.8"/>'
    '<path d="M11.6 11.6l5 5" stroke="{c}" stroke-width="2" stroke-linecap="round"/>',
    "translate": '<text x="0" y="13" font-size="12" font-weight="700" fill="{c}">文</text>'
    '<text x="10" y="17" font-size="10" font-weight="700" fill="{c}">A</text>',
    "summarize": '<path d="M2 4h9M2 9h7M2 14h11" stroke="{c}" stroke-width="1.8" stroke-linecap="round"/>'
    '<path d="M14.5 1l1.2 2.8 2.8 1.2-2.8 1.2-1.2 2.8-1.2-2.8-2.8-1.2 2.8-1.2z" fill="{c}"/>',
    "rewrite": '<path d="M3 15l1-4 9-9 3 3-9 9z" fill="none" stroke="{c}" stroke-width="1.7" stroke-linejoin="round"/>'
    '<path d="M11 4l3 3" stroke="{c}" stroke-width="1.7"/>',
    "explain": '<circle cx="9" cy="9" r="7.5" fill="none" stroke="{c}" stroke-width="1.7"/>'
    '<text x="9" y="13.2" font-size="11" font-weight="700" fill="{c}" text-anchor="middle">?</text>',
    "terminal": '<rect x="1" y="2" width="16" height="14" rx="2.5" fill="none" stroke="{c}" stroke-width="1.7"/>'
    '<path d="M4.5 6.5l3 2.5-3 2.5M9 12h4" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round"/>',
}
BTN, GAP, PAD = 34, 4, 5


def bar_width(n) -> float:
    return PAD * 2 + n * BTN + (n - 1) * GAP


def bar(x, y, buttons, more) -> str:
    """The popup bar: icon buttons, then a muted "+N" chip for the overflow."""
    n = len(buttons) + 1
    out = [
        f'<rect x="{x}" y="{y}" width="{bar_width(n)}" height="{BTN + PAD * 2}" rx="12" fill="{BAR}" stroke="{RIM}"/>'
    ]
    for i, name in enumerate(buttons):
        bx = x + PAD + i * (BTN + GAP)
        out.append(
            f'<g transform="translate({bx + 8},{y + PAD + 8})">{GLYPHS[name].format(c=INK)}</g>'
        )
    cx = x + PAD + len(buttons) * (BTN + GAP) + BTN / 2
    out.append(text(cx, y + PAD + 22, f"+{more}", 13, MUTED, 600, "middle"))
    return "".join(out)


def hover(tl, t0, t1, x, y, i, name, tip) -> str:
    """Button i of the bar at (x, y) lit green, with its tooltip under it."""
    bx = x + PAD + i * (BTN + GAP)
    tw = len(tip) * 7.2 + 20
    return (
        f'<g class="a" {tl.show(t0, t1, 0.12)}><rect x="{bx}" y="{y + PAD}" width="{BTN}" height="{BTN}" rx="8" fill="{GREEN}"/>'
        f'<g transform="translate({bx + 8},{y + PAD + 8})">{GLYPHS[name].format(c="#fff")}</g>'
        f'<rect x="{bx + BTN / 2 - tw / 2}" y="{y + BTN + PAD * 2 + 6}" width="{tw}" height="24" rx="5" fill="#1b1e20" stroke="#4d5359"/>'
        + text(bx + BTN / 2, y + BTN + PAD * 2 + 22, tip, 12.5, "#dfe1e3", anchor="middle")
        + "</g>"
    )


def popup() -> str:
    tl = Timeline(18)
    ex, ey, ew, eh = 70, 30, 820, 440
    left, top, lh = ex + 60, ey + 72, 26
    lines = [
        ("# Standup, Oct 19", "#56cc78"),
        ("", None),
        ("The NPU kept segfaulting after long quantization runs. It turned", None),
        ("out the kernel module leaks state between sessions, and reloading", None),
        ("it clears that. vinoPop now probes the NPU in a subprocess before", None),
        ("loading a model, and falls back to the GPU instead of crashing.", None),
        ("", None),
        ("To reload it by hand:", None),
        ("sudo modprobe -r intel_vpu && sudo modprobe intel_vpu", "#f0c674"),
    ]
    out = [desktop(), editor(ex, ey, ew, eh, "standup.md  ·  Kate")]
    marks: list[str] = []
    for i in range(len(lines) + 4):
        out.append(
            text(
                ex + 36, top + i * lh, str(i + 1), 12, "#6e7479", anchor="end", extra='class="mono"'
            )
        )
    base = lambda i: top + i * lh  # noqa: E731

    def highlight(i, chars, t0, t1):
        return (
            f'<rect class="a" {tl.show(t0, t1)} x="{left - 1}" y="{base(i) - 16}" width="{chars * CHAR + 2:.1f}" '
            'height="22" fill="#2f6e4a"/>'
        )

    # Phase one: drag over the paragraph, Summarize, the answer streams in.
    sel_start, per_line, sel_lines = 1.2, 0.5, [2, 3, 4, 5]
    sel_end = sel_start + per_line * len(sel_lines)
    clear1 = 9.6
    path = [(sel_start - 0.6, left + 120, base(2) + 40), (sel_start, left, base(2) - 6)]
    for k, i in enumerate(sel_lines):
        n = len(lines[i][0])
        t_line = sel_start + k * per_line
        for s in range(1, 9):
            t0 = t_line + per_line * (s - 1) / 8
            t1 = clear1 if s == 8 else t_line + per_line * s / 8
            marks.append(highlight(i, round(n * s / 8), t0, t1))
        path.append((t_line + per_line, left + n * CHAR, base(i) - 6))
    bx, by = left + 150, base(2) - 16 - 10 - (BTN + PAD * 2)
    buttons = ["copy", "search", "translate", "summarize", "rewrite", "explain"]
    click = sel_end + 1.4
    sx = bx + PAD + 3 * (BTN + GAP) + BTN / 2
    path += [
        (sel_end + 0.5, left + 560, base(5) - 6),
        (click - 0.3, sx + 2, by + 26),
        (click + 2, sx + 2, by + 26),
    ]
    out.append(
        f'<g class="a" {tl.show(sel_end + 0.2, click, 0.15, 6)}>{bar(bx, by, buttons, 9)}</g>'
    )
    out.append(hover(tl, click - 0.45, click, bx, by, 3, "summarize", "Summarize"))

    answer = (
        "Long quantization runs left the NPU's kernel module in a bad state. "
        "Reloading the module fixes it, and vinoPop now checks the NPU first and "
        "uses the GPU when it is stuck."
    )
    summary = answer.split()
    rows, row, width = [], [], 0
    for word in summary:
        if width + len(word) > 50 and row:
            rows.append(row)
            row, width = [], 0
        row.append(word)
        width += len(word) + 1
    rows.append(row)
    qx, qy, qw = left + 60, base(5) + 22, 470
    qh = 44 + len(rows) * 22 + 44
    stream_at = click + 0.9
    step = (clear1 - stream_at - 1.6) / len(summary)
    out.append(
        f'<g class="a" {tl.show(click + 0.1, clear1, 0.2, 8)}><rect x="{qx}" y="{qy}" width="{qw}" height="{qh}" rx="14" '
        f'fill="#2a2e32" stroke="#ffffff" stroke-opacity=".12"/>'
        + text(qx + 14, qy + 24, "Summarize", 12, MUTED)
        + "</g>"
    )
    out.append(
        f'<g class="a" {tl.show(click + 0.1, stream_at, 0.2)}>'
        + text(qx + 14, qy + 52, "Thinking…", 15, MUTED)
        + "</g>"
    )
    shown = 0
    for r, words in enumerate(rows):
        for j in range(1, len(words) + 1):
            t0 = stream_at + (shown + j - 1) * step
            t1 = clear1 if j == len(words) else stream_at + (shown + j) * step
            caret = "" if j == len(words) else '<tspan class="caret" fill="#56cc78"> ▍</tspan>'
            out.append(
                f'<text class="a" {tl.show(t0, t1)} x="{qx + 14}" y="{qy + 52 + r * 22}" font-size="15" fill="#eff0f1">'
                f"{escape(' '.join(words[:j]))}{caret}</text>"
            )
        shown += len(words)
    done_at = stream_at + len(summary) * step
    chips = "".join(
        f'<rect x="{qx + 14 + i * 84}" y="{qy + qh - 36}" width="74" height="24" rx="6" fill="#3a3f44"/>'
        + text(qx + 51 + i * 84, qy + qh - 19, label, 12.5, "#dfe1e3", anchor="middle")
        for i, label in enumerate(["Copy", "Replace"])
    )
    out.append(f'<g class="a" {tl.show(done_at, clear1, 0.2)}>{chips}</g>')

    # Phase two: select the command, the bar offers to run it.
    cmd = lines[8][0]
    s2 = clear1 + 0.8
    path += [(clear1, sx + 40, by + 120), (s2, left, base(8) - 6)]
    for s in range(1, 9):
        t0 = s2 + 0.6 * (s - 1) / 8
        marks.append(
            highlight(
                8, round(len(cmd) * s / 8), t0, tl.length - 0.4 if s == 8 else s2 + 0.6 * s / 8
            )
        )
    s2_end = s2 + 0.6
    path.append((s2_end, left + len(cmd) * CHAR, base(8) - 6))
    b2x, b2y = left + 140, base(8) - 16 - 10 - (BTN + PAD * 2)
    run_x = b2x + PAD + 1 * (BTN + GAP) + BTN / 2
    path += [(s2_end + 1.1, run_x + 2, b2y + 26), (tl.length, run_x + 2, b2y + 26)]
    out.append(
        f'<g class="a" {tl.show(s2_end + 0.2, tl.length - 0.4, 0.15, 6)}>{bar(b2x, b2y, ["copy", "terminal", "search"], 5)}</g>'
    )
    out.append(hover(tl, s2_end + 1.2, tl.length - 0.4, b2x, b2y, 1, "terminal", "Run in terminal"))

    out[2:2] = marks + [
        mono(left, base(i), s, colour or "#dfe1e3") for i, (s, colour) in enumerate(lines) if s
    ]
    out.append(cursor(tl, path))
    return svg(
        tl,
        out,
        "vinoPop: selecting a paragraph pops a bar of buttons, Summarize streams a summary from the local model, then selecting a shell command offers Run in terminal",
    )


def clipboard() -> str:
    tl = Timeline(13)
    ex, ey, ew, eh = 70, 30, 820, 440
    left, top, lh = ex + 60, ey + 72, 26
    draft = [
        "Hi Sam,",
        "",
        "The release is tagged and the notes are up. Shout if the",
        "installer gives you any trouble.",
        "",
    ]
    out = [desktop(), editor(ex, ey, ew, eh, "reply.txt  ·  Kate")]
    for i in range(12):
        out.append(
            text(
                ex + 36, top + i * lh, str(i + 1), 12, "#6e7479", anchor="end", extra='class="mono"'
            )
        )
    for i, s in enumerate(draft):
        if s:
            out.append(mono(left, top + i * lh, s))
    open_at, tab_at, enter_at = 1.0, 4.6, 8.6
    caret_y = top + 5 * lh
    out.append(
        f'<g class="a" {tl.show(0, enter_at + 0.2)}><rect class="caret" x="{left}" y="{caret_y - 16}" width="2" height="20" fill="#dfe1e3"/></g>'
    )
    out.append(keycap(tl, 0.2, open_at + 0.3, "Super+Shift+V", 132))

    px, py, pw, ph = 330, 70, 460, 400
    picker = (
        f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="12" fill="#242428" stroke="#3a3d42"/>'
        f'<svg x="{px + 12}" y="{py + 10}" width="24" height="24" viewBox="2 2 60 60">{icon()}</svg>'
        + text(px + 44, py + 27, "vinoPop", 14, INK, 700)
        + text(px + 106, py + 27, "Clipboard & snippets", 13, MUTED)
        + f'<rect x="{px + 10}" y="{py + 42}" width="{pw - 20}" height="34" rx="7" fill="#1b1b1e" stroke="#3a3d42"/>'
        + f'<circle cx="{px + 28}" cy="{py + 58}" r="5.5" fill="none" stroke="{MUTED}" stroke-width="1.8"/>'
        + f'<path d="M{px + 32} {py + 62}l4 4" stroke="{MUTED}" stroke-width="2" stroke-linecap="round"/>'
        + f'<rect x="{px + 10}" y="{py + 118}" width="{pw - 20}" height="1" fill="#3a3d42"/>'
        + text(
            px + 12,
            py + ph - 14,
            "Enter to paste · Tab to switch tab · Ctrl+P pin · Esc to close",
            11.5,
            MUTED,
        )
    )
    shown = (open_at, enter_at)
    out.append(f'<g class="a" {tl.show(*shown, 0.15, 8)}>{picker}</g>')

    def tabs(active, t0, t1):
        g = []
        for i, label in enumerate(["Recent", "Snippets"]):
            tx = px + 20 + i * 96
            colour = INK if i == active else MUTED
            g.append(
                text(tx + 38, py + 108, label, 13.5, colour, 600 if i == active else 400, "middle")
            )
            if i == active:
                g.append(
                    f'<rect x="{tx}" y="{py + 115}" width="76" height="3" rx="1.5" fill="#56cc78"/>'
                )
        return f'<g class="a" {tl.show(t0, t1)}>{"".join(g)}</g>'

    out.append(tabs(0, open_at, tab_at))
    out.append(tabs(1, tab_at, enter_at))

    def entry_row(y, title, sub, selected, pinned=False, thumb=False):
        g = ""
        if selected:
            g += f'<rect x="{px + 10}" y="{y}" width="{pw - 20}" height="48" rx="7" fill="#56cc78" fill-opacity=".14"/>'
        tx = px + 22
        if thumb:
            g += (
                f'<rect x="{tx}" y="{y + 8}" width="48" height="32" rx="4" fill="#2c6e8f"/>'
                f'<circle cx="{tx + 12}" cy="{y + 17}" r="4" fill="#f6d25c"/>'
                f'<path d="M{tx} {y + 40}l16-14 10 8 8-6 14 12z" fill="#e8f5ee"/>'
            )
            tx += 60
        g += text(
            tx,
            y + 21,
            title,
            14,
            INK,
            extra='class="mono"' if title.startswith((";", "sudo", "http")) else "",
        )
        g += text(tx, y + 39, sub, 12, MUTED)
        if pinned:
            g += text(px + pw - 24, y + 21, "pinned", 11.5, "#56cc78", 600, "end")
        return g

    recent = [
        ("https://github.com/karanshukla/vinoPop", "Copied 2 minutes ago", True, False),
        ("Screenshot 412 × 230", "Image · 5 minutes ago", False, True),
        ("sudo modprobe -r intel_vpu", "Copied 20 minutes ago", False, False),
        ("Meeting moved to 3:30, same room", "Copied yesterday", True, False),
    ]
    sel_times = [open_at, open_at + 1.2, open_at + 2.0, open_at + 2.8, tab_at]
    for s in range(4):
        g = "".join(
            entry_row(py + 128 + i * 54, t, sub, i == s, pinned, thumb)
            for i, (t, sub, pinned, thumb) in enumerate(recent)
        )
        out.append(f'<g class="a" {tl.show(sel_times[s], sel_times[s + 1])}>{g}</g>')

    query = "sig"
    type_at, step = tab_at + 1.0, 0.25
    snippets = [
        (";sig", "Thanks, Karan · {date}"),
        (";standup", "Yesterday / Today / Blocked"),
        (";addr", "Home address"),
        (";sign-off", "Cheers, Karan"),
    ]
    out.append(
        f'<g class="a" {tl.show(open_at, type_at)}>{text(px + 44, py + 64, "Type to filter…", 14, "#6e7479")}</g>'
    )
    out.append(
        f'<g class="a" {tl.show(tab_at, type_at)}>'
        + "".join(
            entry_row(py + 128 + i * 54, t, sub, i == 0) for i, (t, sub) in enumerate(snippets)
        )
        + "</g>"
    )
    for i in range(1, len(query) + 1):
        t0 = type_at + (i - 1) * step
        t1 = enter_at if i == len(query) else type_at + i * step
        out.append(
            f'<g class="a" {tl.show(t0, t1)}>{text(px + 44, py + 64, query[:i], 14, INK)}'
            f'<rect class="caret" x="{px + 46 + i * 7.4:.1f}" y="{py + 50}" width="1.6" height="18" fill="#56cc78"/></g>'
        )
        keep = [s for s in snippets if query[:i] in s[0] or query[:i] in s[1].lower()]
        rows = "".join(
            entry_row(py + 128 + j * 54, t, sub, j == 0) for j, (t, sub) in enumerate(keep)
        )
        out.append(f'<g class="a" {tl.show(t0, t1)}>{rows}</g>')
    out.append(keycap(tl, tab_at - 0.7, tab_at + 0.3, "Tab", 56))
    out.append(keycap(tl, enter_at - 0.8, enter_at + 0.3, "Enter", 72))

    # The snippet lands where the caret was, {date} filled in.
    pasted = ["Thanks,", "Karan", "2026-10-19"]
    for i, s in enumerate(pasted):
        out.append(
            f'<g class="a" {tl.show(enter_at + 0.15, tl.length - 0.3, 0.15)}>{mono(left, caret_y + i * lh, s)}</g>'
        )
    end_x = left + len(pasted[-1]) * CHAR + 2
    out.append(
        f'<g class="a" {tl.show(enter_at + 0.15, tl.length - 0.3)}><rect class="caret" x="{end_x}" '
        f'y="{caret_y + 2 * lh - 16}" width="2" height="20" fill="#dfe1e3"/></g>'
    )
    out.append(
        f'<g class="a" {tl.show(enter_at + 0.5, tl.length - 0.3, 0.3)}><rect x="{left + 140}" y="{caret_y + 24}" width="196" height="28" rx="6" '
        f'fill="#56cc78" fill-opacity=".14" stroke="#56cc78" stroke-opacity=".5"/>'
        + text(left + 238, caret_y + 43, "{date} filled in", 13, "#56cc78", 600, "middle")
        + "</g>"
    )
    return svg(
        tl,
        out,
        "vinoPop clipboard history: Super+Shift+V opens recent copies, Tab switches to snippets, typing sig filters them, and Enter pastes a signature with today's date",
    )


def icon() -> str:
    logo = Path(__file__).resolve().parents[2] / "icons" / "vinopop.svg"
    return logo.read_text().split(">", 1)[1].rsplit("</svg>", 1)[0]


def svg(tl: Timeline, body: list[str], title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>'
        f"{tl.css()}{''.join(body)}</svg>\n"
    )


if __name__ == "__main__":
    here = Path(__file__).parent
    (here / "popup.svg").write_text(popup())
    (here / "clipboard.svg").write_text(clipboard())
