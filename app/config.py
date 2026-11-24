"""Runtime configuration.

The SQLite file location can be overridden with the ``GAPCHECK_DB_PATH``
environment variable (tests use it to point at a temporary database).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("GAPCHECK_DB_PATH", "/data/gapcheck.db"))

# A mask and a query rectangle are each at most 256 x 256 cells.
MAX_SIDE = 256
