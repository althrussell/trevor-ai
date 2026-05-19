"""UC Volume-backed HERMES_HOME mirror (Phase 1 skeleton).

The real implementation arrives in Phase 5.
"""

from __future__ import annotations

from hermes_databricks.config import Config


class UCVolumeHome:  # pragma: no cover - Phase 1 stub
    @classmethod
    def from_config(cls, cfg: Config) -> "UCVolumeHome":
        raise NotImplementedError("UCVolumeHome is implemented in Phase 5")

    def ensure_local_dirs(self) -> None:
        raise NotImplementedError("UCVolumeHome is implemented in Phase 5")

    def sync_from_volume(self) -> None:
        raise NotImplementedError("UCVolumeHome is implemented in Phase 5")

    def sync_to_volume(self) -> None:
        return None

    def list(self, path: str = "") -> list:
        return []

    def status(self) -> dict:
        return {"status": "unavailable", "reason": "Phase 5 not yet wired"}

    def close(self) -> None:
        return None

    async def health_probe(self) -> dict:
        return {"status": "unavailable", "reason": "Phase 5 not yet wired"}
