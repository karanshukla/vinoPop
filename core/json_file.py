"""Rewriting a JSON file in place without ever leaving half of one."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.xdg_paths import open_private


def write_json(
    path: Path,
    data: Any,
    *,
    private: bool = False,
    temp_suffix: str = ".tmp",
    ascii_only: bool = False,
    final_newline: bool = True,
    fsync: bool = True,
) -> None:
    """Write `data` beside `path` and rename it over. `private` creates it
    0600 whatever the umask. The folder must exist; a failure raises
    OSError and leaves `path` as it was. tests/test_json_files.py pins what
    each caller's file looks like."""
    tmp = path.with_name(path.name + temp_suffix)
    with open_private(tmp) if private else tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=ascii_only)
        if final_newline:
            f.write("\n")
        if fsync:
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    os.replace(tmp, path)
