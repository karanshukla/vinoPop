"""The vinoPilot apps, their pages on vinopilot.ca, and the tray menu icons
every vinoPilot tray uses.

A copy of vino-ui's ``family`` and ``tray`` modules
(https://github.com/karanshukla/vinoUserInterface, src/family.rs and
src/tray.rs), which the Rust apps use directly. Keep it in step with them:
the same names, blurbs, URLs and icon names, so the family reads the same
from every app.
"""

from __future__ import annotations

from typing import NamedTuple

SITE = "https://vinopilot.ca/"
SITE_LABEL = "vinopilot.ca"


class App(NamedTuple):
    name: str
    blurb: str
    page: str


# In the order vinopilot.ca lists them.
APPS = (
    App(
        "vinoPilot",
        "One app to install and update the rest",
        "https://vinopilot.ca/apps/#vinopilot",
    ),
    App("vinoWhisper", "Live captions and voice typing", "https://vinopilot.ca/apps/#vinowhisper"),
    App("vinoAuthFace", "Unlock with your face", "https://vinopilot.ca/apps/#vinoauthface"),
    App(
        "vinoOpenEffects",
        "Look better on video calls",
        "https://vinopilot.ca/apps/#vinoopeneffects",
    ),
    App("vinoPop", "AI actions on any text you select", "https://vinopilot.ca/apps/#vinopop"),
    App("vinoSpeak", "Natural-sounding text-to-speech", "https://vinopilot.ca/apps/#vinospeak"),
    App("vinoSearch", "Find files by what they're about", "https://vinopilot.ca/apps/#vinosearch"),
    App("vinoWatch", "See what your NPU is doing", "https://vinopilot.ca/apps/#vinowatch"),
)


def others(this: str = "vinoPop") -> list[App]:
    """Every app but ``this``, in the site's order."""
    return [app for app in APPS if app.name != this]


# Tray menu icons by what the item does (vino-ui's ``MenuIcon``): -symbolic
# names that Breeze and Adwaita both ship, so the menu recolours them and
# none is blank on GNOME. Only the ones vinoPop's menu uses.
MENU_ICONS = {
    "settings": "preferences-system-symbolic",
    "quit": "application-exit-symbolic",
    "about": "help-about-symbolic",
    "popup": "open-menu-symbolic",
    "plugins": "preferences-other-symbolic",
}
