"""Keep host tests away from the checkout's real data.

Test modules import ``server`` at collection time, and importing it opens the
domain database when the checkout's ``dashboard-config.json`` enables the
features. Without this, running the suite in a daily-use checkout migrates and
imports into the real ``state/domain.sqlite3``. Set before any test module loads.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile

_ROOT = Path(tempfile.mkdtemp(prefix="kaus-host-tests-"))
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)

os.environ["DASH_DOMAIN_DB"] = str(_ROOT / "domain.sqlite3")
os.environ.pop("DASH_SESSION_API_TOKEN_FILE", None)  # defaults to next to the database
os.environ["KAUS_COMPUTER_USE_HOME"] = str(_ROOT / "computer-use")
os.environ["HERMES_HOME"] = str(_ROOT / "native-home")
os.environ["KAUS_SYSTEM_NOTIFICATIONS"] = "0"  # fake runs must not pop real notifications
(_ROOT / "native-home").mkdir()
