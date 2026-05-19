"""Lakebase-backed Hermes SessionDB adapter (Phase 1 skeleton).

The real implementation arrives in Phase 4. This stub exists so the
runtime can attempt to import it and degrade cleanly when the
dependencies are not yet in place.
"""

from __future__ import annotations

from hermes_databricks.config import Config


class LakebaseSessionDB:  # pragma: no cover - Phase 1 stub
    @classmethod
    def from_config(cls, cfg: Config) -> "LakebaseSessionDB":
        raise NotImplementedError("LakebaseSessionDB is implemented in Phase 4")

    def ensure_schema(self) -> None:
        raise NotImplementedError("LakebaseSessionDB is implemented in Phase 4")

    def close(self) -> None:
        return None

    async def health_probe(self) -> dict:
        return {"status": "unavailable", "reason": "Phase 4 not yet wired"}
