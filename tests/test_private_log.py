"""The log can name what was selected (at --debug), so it stays 0600."""

import logging
import os
import stat

from core.xdg_paths import PrivateRotatingFileHandler


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_log_and_rotated_logs_are_private(tmp_path):
    path = tmp_path / "vinopop.log"
    old = os.umask(0o022)
    try:
        h = PrivateRotatingFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
        log = logging.getLogger("test_private_log")
        log.addHandler(h)
        log.setLevel(logging.INFO)
        try:
            for i in range(20):
                log.info("line %d %s", i, "x" * 40)
        finally:
            log.removeHandler(h)
            h.close()
    finally:
        os.umask(old)
    files = sorted(tmp_path.glob("vinopop.log*"))
    assert len(files) == 3, files  # rotated at least twice
    assert all(_mode(f) == 0o600 for f in files), {f.name: oct(_mode(f)) for f in files}


def test_an_old_world_readable_log_is_made_private(tmp_path):
    path = tmp_path / "vinopop.log"
    path.write_text("old\n")
    path.chmod(0o644)
    h = PrivateRotatingFileHandler(path, encoding="utf-8")
    h.close()
    assert _mode(path) == 0o600
    assert path.read_text() == "old\n"  # appended to, not truncated
