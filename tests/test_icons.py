"""Icon names stay renderable on both Adwaita (GNOME) and Breeze (KDE)."""

import configparser
import json
import re
from pathlib import Path

import pytest

import icon_style

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "icons"

# System icons confirmed present in BOTH Adwaita and Breeze. Using anything
# else means a blank button on one desktop: add it to icon_style._FALLBACKS
# (runtime swap) or pick a name from this list. Checked against whichever of
# the two is installed by test_shipped_by_both_matches_installed_themes.
SHIPPED_BY_BOTH = {
    "accessories-dictionary-symbolic",
    "alarm-symbolic",
    "audio-speakers-symbolic",
    "camera-photo-symbolic",
    "color-select-symbolic",
    "dialog-information-symbolic",
    "dialog-password-symbolic",
    "dialog-question-symbolic",
    "document-edit-symbolic",
    "document-new-symbolic",
    "edit-clear-all-symbolic",
    "edit-clear-symbolic",
    "edit-copy-symbolic",
    "edit-cut-symbolic",
    "edit-find-symbolic",
    "edit-paste-symbolic",
    "edit-select-all-symbolic",
    "emblem-system-symbolic",
    "emoji-people-symbolic",
    "face-smile-big-symbolic",
    "folder-open-symbolic",
    "folder-pictures-symbolic",
    "folder-symbolic",
    "format-justify-fill-symbolic",
    "format-justify-left-symbolic",
    "format-text-bold-symbolic",
    "format-text-italic-symbolic",
    "format-text-strikethrough-symbolic",
    "format-text-underline-symbolic",
    "go-down-symbolic",
    "go-up-symbolic",
    "help-about-symbolic",
    "input-keyboard-symbolic",
    "input-mouse-symbolic",
    "insert-link-symbolic",
    "insert-object-symbolic",
    "list-add-symbolic",
    "mail-send-symbolic",
    "mark-location-symbolic",
    "media-playback-start-symbolic",
    "network-workgroup-symbolic",
    "non-starred-symbolic",
    "object-flip-horizontal-symbolic",
    "pan-down-symbolic",
    "preferences-other-symbolic",
    "preferences-system-symbolic",
    "security-high-symbolic",
    "security-medium-symbolic",
    "starred-symbolic",
    "system-run-symbolic",
    "system-search-symbolic",
    "text-x-generic-symbolic",
    "user-trash-symbolic",
    "view-fullscreen-symbolic",
    "view-grid-symbolic",
    "view-list-symbolic",
    "view-more-symbolic",
    "view-refresh-symbolic",
    "view-sort-ascending-symbolic",
    "zoom-in-symbolic",
}


class _Theme:
    def __init__(self, *names):
        self.names = set(names)

    def has_icon(self, name):
        return name in self.names


def _declared_icons() -> set[str]:
    found: set[str] = set()
    sources = list((ROOT / "plugins_repo").glob("*.py"))
    sources += [ROOT / n for n in ("plugin_loader.py", "recipe_wizard.py", "actions.py")]
    for path in sources:
        found |= set(re.findall(r"[\"']([a-z][a-z0-9-]*-symbolic)[\"']", path.read_text()))
    for path in (ROOT / "plugins_repo").rglob("*.json"):
        data = json.loads(path.read_text())
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("icon"), str):
                found.add(entry["icon"])
    return found


def test_declared_icons_exist_on_both_desktops():
    bundled = {p.stem for p in ICONS.glob("*.svg")}
    risky = {
        n
        for n in _declared_icons()
        if n not in bundled and n not in SHIPPED_BY_BOTH and n not in icon_style._FALLBACKS
    }
    assert not risky, f"icons missing from Adwaita and/or Breeze with no fallback: {sorted(risky)}"


def test_fallback_targets_are_themselves_safe():
    for name, alternatives in icon_style._FALLBACKS.items():
        assert any(alt in SHIPPED_BY_BOTH for alt in alternatives), name


def test_bundled_icons_come_in_colour_and_glyph_pairs():
    for concept in set(icon_style._CONCEPT_BY_ICON.values()):
        assert (ICONS / f"vinopop-{concept}.svg").is_file(), concept
        assert (ICONS / f"vinopop-{concept}-symbolic.svg").is_file(), concept


@pytest.mark.parametrize("style,suffix", [("color", ""), ("glyph", "-symbolic")])
def test_resolve_accepts_bare_concept_names(style, suffix):
    # "vinopop-maps" used to be sliced one character too far and never matched
    assert icon_style.resolve("vinopop-maps", style) == f"vinopop-maps{suffix}"
    assert icon_style.resolve("vinopop-google", style) == f"vinopop-google{suffix}"


def test_resolve_maps_system_names_to_concepts():
    assert icon_style.resolve("mark-location-symbolic", "glyph") == "vinopop-maps-symbolic"
    assert icon_style.resolve("mark-location-symbolic", "color") == "vinopop-maps"


def test_available_swaps_in_a_shipped_alternative():
    breeze_like = _Theme("network-workgroup-symbolic")
    assert icon_style.available("web-browser-symbolic", breeze_like) == "network-workgroup-symbolic"


def test_available_leaves_present_and_unknown_names_alone():
    theme = _Theme("web-browser-symbolic")
    assert icon_style.available("web-browser-symbolic", theme) == "web-browser-symbolic"
    assert icon_style.available("some-custom-icon", theme) == "some-custom-icon"
    assert icon_style.available("vinopop-maps", theme) == "vinopop-maps"
    assert icon_style.available("", theme) == ""


# Adwaita 51 dropped these; it's the icon theme on KDE in dark mode (theme.py)
_ADWAITA_51_GAPS = {
    "tag-symbolic": "text-x-generic-symbolic",
    "emblem-default-symbolic": "starred-symbolic",
    "emblem-favorite-symbolic": "starred-symbolic",
}


@pytest.mark.parametrize("name,expected", _ADWAITA_51_GAPS.items())
def test_icons_newer_adwaita_lacks_fall_back(name, expected):
    adwaita_51 = _Theme(*SHIPPED_BY_BOTH)
    assert icon_style.available(name, adwaita_51) == expected


def test_emblem_default_prefers_a_check_mark_when_there_is_one():
    theme = _Theme("object-select-symbolic", "starred-symbolic")
    assert icon_style.available("emblem-default-symbolic", theme) == "object-select-symbolic"


def _theme_icon_names(theme: str, seen: set[str] | None = None) -> set[str] | None:
    """Every icon name `theme` (and the themes it inherits) ships, read from
    /usr/share/icons; None when it isn't installed."""
    seen = seen if seen is not None else set()
    base = Path("/usr/share/icons") / theme
    index = base / "index.theme"
    if theme in seen or not index.is_file():
        return None
    seen.add(theme)
    names = {p.name.rsplit(".", 1)[0] for p in base.rglob("*") if p.suffix in (".svg", ".png")}
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cfg.read(index, encoding="utf-8")
        parents = cfg.get("Icon Theme", "Inherits", fallback="")
    except configparser.Error:
        parents = ""
    for parent in filter(None, (p.strip() for p in parents.split(","))):
        names |= _theme_icon_names(parent, seen) or set()
    return names


@pytest.mark.parametrize("theme", ["Adwaita", "breeze"])
def test_shipped_by_both_matches_installed_themes(theme):
    names = _theme_icon_names(theme)
    if names is None:
        pytest.skip(f"{theme} icon theme not installed")
    # GTK falls back from foo-symbolic to foo, so either name draws
    missing = sorted(
        n for n in SHIPPED_BY_BOTH if n not in names and n.removesuffix("-symbolic") not in names
    )
    assert not missing, f"{theme} lacks {missing}: drop them from SHIPPED_BY_BOTH"


def test_picker_suggestions_only_offer_icons_the_theme_draws():
    pytest.importorskip("gi")
    try:
        import icon_picker
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    breeze_like = _Theme("network-workgroup-symbolic", "system-search-symbolic", "starred-symbolic")
    got = icon_picker._suggestions(breeze_like)
    assert "web-browser-symbolic" not in got  # would show image-missing
    assert got[0] == "network-workgroup-symbolic"
    assert all(breeze_like.has_icon(n) for n in got)
    assert len(got) == len(set(got))


class _Settings:
    def __init__(self):
        self.icon_theme = "breeze"
        self.calls = []

    def set_property(self, name, value):
        self.calls.append(("set", name, value))
        self.icon_theme = value

    def reset_property(self, name):
        self.calls.append(("reset", name))
        self.icon_theme = "breeze"  # back to the session's own theme


def test_kde_icon_theme_follows_the_mode_both_ways():
    pytest.importorskip("gi")
    try:
        import theme
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    s = _Settings()
    theme._sync_icon_theme("dark", s, "wayland_kde")
    assert s.icon_theme == "Adwaita"
    # switching to light used to leave Adwaita forced until a restart
    theme._sync_icon_theme("light", s, "wayland_kde")
    assert s.icon_theme == "breeze"
    other = _Settings()
    theme._sync_icon_theme("dark", other, "x11")
    assert other.calls == []
