"""Monitor lookup and coordinate conversion shared by the popup, the AI
bubble, the OCR overlay and the layer-shell placement code.

Pure duck-typed helpers (no gi import) so the maths is unit-testable with
fake monitors. A "monitor" is anything with get_geometry() returning an
object with x/y/width/height, and get_scale_factor(); a monitor list is a
Gio.ListModel (get_n_items/get_item) or a plain sequence.

Coordinate spaces: GDK monitor geometry is logical (global, compositor
pixels). KWin's cursor and AT-SPI coords are logical too; X11 root coords
are physical, so they are divided by the monitor's scale before testing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _iter_monitors(monitors: Any) -> Iterable[Any]:
    if monitors is None:
        return []
    if hasattr(monitors, "get_n_items"):
        return [monitors.get_item(i) for i in range(monitors.get_n_items())]
    return list(monitors)


def _contains(geom: Any, x: float, y: float) -> bool:
    return geom.x <= x < geom.x + geom.width and geom.y <= y < geom.y + geom.height


def _scale(monitor: Any) -> int:
    try:
        return max(1, int(monitor.get_scale_factor()))
    except Exception:
        return 1


def monitor_at(monitors: Any, x: float, y: float, is_logical: bool = True, fallback: bool = True):
    """The monitor containing (x, y). With `fallback`, the first monitor
    when none contains it (so callers can still clamp on-screen); without,
    None (so off-screen parking points aren't pinned to an output)."""
    first = None
    for m in _iter_monitors(monitors):
        if first is None:
            first = m
        s = 1 if is_logical else _scale(m)
        if _contains(m.get_geometry(), x / s, y / s):
            return m
    return first if fallback else None


def to_logical(x: float, y: float, monitor: Any, is_logical: bool) -> tuple[float, float]:
    """Convert backend pointer coords to logical: identity when the backend
    already reports logical coords, else divide by the monitor's scale."""
    if is_logical or monitor is None:
        return float(x), float(y)
    s = _scale(monitor)
    return x / s, y / s


def layer_margins(x: int, y: int, geom: Any) -> tuple[int, int]:
    """Layer-shell LEFT/TOP margins for global logical (x, y) on the output
    whose geometry is `geom`. Margins are relative to the surface's own
    output, so a second monitor at x=1920 must subtract its origin."""
    return int(x) - int(geom.x), int(y) - int(geom.y)
