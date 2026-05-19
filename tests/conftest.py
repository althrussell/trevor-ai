"""Shared pytest configuration.

Ensures the ``app/`` source tree is on ``sys.path`` so ``import
hermes_databricks`` works without installing the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_APP_SRC = _REPO / "app"

for entry in (_APP_SRC,):
    s = str(entry)
    if s not in sys.path:
        sys.path.insert(0, s)
