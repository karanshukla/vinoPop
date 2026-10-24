"""Which distro family this is, whether its /usr is read-only, and how to
install a package there.

The OCR Install button runs the install itself through pkexec, which only
works on a distro whose package manager writes to the running system. On
Fedora Atomic (Silverblue, Kinoite, Bazzite) `dnf install` refuses and
packages are layered with rpm-ostree; on openSUSE Aeon and MicroOS they go
through transactional-update. Both need a reboot (or rpm-ostree's
--apply-live), so there the user gets the command instead of a button.
"""

from __future__ import annotations

import os
from pathlib import Path

FEDORA, DEBIAN, ARCH, SUSE = "fedora", "debian", "arch", "suse"

# os-release ID / ID_LIKE values -> family. ID_LIKE covers most derivatives
# (Pop!_OS says "ubuntu debian", Bazzite says "fedora").
_FAMILIES = {
    "fedora": FEDORA,
    "rhel": FEDORA,
    "centos": FEDORA,
    "rocky": FEDORA,
    "almalinux": FEDORA,
    "nobara": FEDORA,
    "bazzite": FEDORA,
    "debian": DEBIAN,
    "ubuntu": DEBIAN,
    "pop": DEBIAN,
    "linuxmint": DEBIAN,
    "arch": ARCH,
    "manjaro": ARCH,
    "endeavouros": ARCH,
    "cachyos": ARCH,
    "suse": SUSE,
    "opensuse": SUSE,
    "opensuse-tumbleweed": SUSE,
    "opensuse-leap": SUSE,
    "opensuse-microos": SUSE,
    "opensuse-aeon": SUSE,
    "opensuse-kalpa": SUSE,
}

OSTREE = "ostree"  # Fedora Atomic: rpm-ostree
TRANSACTIONAL = "transactional"  # openSUSE Aeon / MicroOS / Kalpa
READ_ONLY = "read-only"  # /usr is read-only and we don't know the tool

_ST_RDONLY = getattr(os, "ST_RDONLY", 1)


def os_release(root: Path = Path("/")) -> dict[str, str]:
    for rel in ("etc/os-release", "usr/lib/os-release"):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        values = {}
        for line in text.splitlines():
            key, sep, value = line.strip().partition("=")
            if sep:
                values[key] = value.strip().strip("\"'")
        return values
    return {}


def family(root: Path = Path("/")) -> str:
    """fedora, debian, arch or suse; "" when unrecognised."""
    info = os_release(root)
    for candidate in [info.get("ID", "")] + info.get("ID_LIKE", "").split():
        fam = _FAMILIES.get(candidate.lower())
        if fam:
            return fam
    return ""


def _usr_read_only(root: Path) -> bool:
    try:
        return bool(os.statvfs(root / "usr").f_flag & _ST_RDONLY)
    except OSError:
        return False


def immutable(root: Path = Path("/")) -> str:
    """OSTREE, TRANSACTIONAL or READ_ONLY on an image-based distro; "" on a
    normal one. /run/ostree-booted is what rpm-ostree itself checks."""
    if (root / "run/ostree-booted").exists():
        return OSTREE
    if not _usr_read_only(root):
        return ""
    if (root / "usr/sbin/transactional-update").exists():
        return TRANSACTIONAL
    return READ_ONLY


_INSTALL = {
    FEDORA: ["dnf", "install", "-y"],
    DEBIAN: ["apt-get", "install", "-y"],
    ARCH: ["pacman", "-S", "--needed"],
    SUSE: ["zypper", "install"],
}
# pkexec runs with no terminal to answer a prompt on.
_NO_PROMPT = {ARCH: ["--noconfirm"]}


def install_command(packages: list[str], root: Path = Path("/"), fam: str | None = None) -> str:
    """The command a user should run to install `packages`. `fam` overrides
    the family read from os-release."""
    pkgs = " ".join(packages)
    kind = immutable(root)
    if kind == OSTREE:
        return f"rpm-ostree install {pkgs}, then reboot"
    if kind == TRANSACTIONAL:
        return f"sudo transactional-update pkg install {pkgs}, then reboot"
    if kind == READ_ONLY:
        return f"install {pkgs} with your distro's image tool (/usr is read-only)"
    argv = _INSTALL.get(family(root) if fam is None else fam)
    if argv is None:
        return f"install {pkgs} with your package manager"
    return "sudo " + " ".join(argv + packages)


def install_argv(packages: list[str], root: Path = Path("/")) -> list[str] | None:
    """A pkexec argv that installs `packages` in place, or None where that
    can't work: an unknown package manager, or a read-only /usr where the
    install needs a new deployment and a reboot."""
    if immutable(root):
        return None
    fam = family(root)
    argv = _INSTALL.get(fam)
    if argv is None:
        return None
    if fam == SUSE:  # a global option: before the subcommand
        return ["pkexec", "zypper", "--non-interactive", "install", *packages]
    return ["pkexec", *argv, *_NO_PROMPT.get(fam, []), *packages]
