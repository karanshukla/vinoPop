"""The AppStream metainfo GNOME Software and KDE Discover read: it names the
app ID the launcher uses and lists the releases the changelog has."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.github.karanshukla.vinoPop"
METAINFO = REPO / "data" / f"{APP_ID}.metainfo.xml"


def _root():
    return ET.parse(METAINFO).getroot()


def test_ids_match_install_sh():
    install_sh = (REPO / "install.sh").read_text()
    assert f'APP_ID="{APP_ID}"' in install_sh
    root = _root()
    assert root.get("type") == "desktop-application"
    assert root.findtext("id") == APP_ID
    assert root.findtext("launchable") == f"{APP_ID}.desktop"


def test_releases_follow_the_changelog():
    changelog = (REPO / "CHANGELOG.md").read_text()
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})", changelog, re.M)
    listed = [(r.get("version"), r.get("date")) for r in _root().iter("release")]
    # Newest first, as AppStream wants, and none missing
    assert listed == released
