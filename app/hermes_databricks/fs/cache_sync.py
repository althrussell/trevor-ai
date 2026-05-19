"""Background cache → UC Volume sync (Phase 1 skeleton; full in Phase 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class CacheSync:  # pragma: no cover - Phase 1 stub
    def __init__(self, local_root: Path, volume_root: str, durable_subpaths: Iterable[str]) -> None:
        self.local_root = local_root
        self.volume_root = volume_root
        self.durable_subpaths = list(durable_subpaths)

    def sync_now(self) -> None:
        return None
