import os
import sys
import tempfile
from pathlib import Path

# Keep settings.json and caches out of the real home; xdg_paths reads these at import
_TMP = tempfile.mkdtemp(prefix="vinopop-tests-")
for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    os.environ[var] = os.path.join(_TMP, var.lower())

# Plugins import core modules by bare name, as they do once installed
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins_repo"))
