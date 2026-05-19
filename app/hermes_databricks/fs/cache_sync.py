"""Tiny adapter that the supervisor can use to trigger UC Volume syncs.

This lives next to ``UCVolumeHome`` so that callers don't import
``WorkspaceClient`` directly; the supervisor only needs to call
``cache_sync.sync_now()`` from its heartbeat task.
"""

from __future__ import annotations

import logging
from typing import Optional

from hermes_databricks.fs.volume_fs import UCVolumeHome


log = logging.getLogger("hermes_databricks.fs.cache_sync")


class CacheSync:
    """Light wrapper that runs ``UCVolumeHome.sync_to_volume`` on demand."""

    def __init__(self, home: UCVolumeHome) -> None:
        self.home = home
        self._last_result: Optional[dict] = None

    def sync_now(self, *, only_durable: bool = True) -> dict:
        self._last_result = self.home.sync_to_volume(only_durable=only_durable)
        return self._last_result

    def status(self) -> dict:
        return {
            "last": self._last_result,
            "home_status": self.home.status(),
        }
