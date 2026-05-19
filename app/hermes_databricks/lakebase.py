"""Lakebase connection helper (Phase 1 skeleton; full pool in Phase 4).

Implements the same credential-rotation pattern Living-AI uses, but
is not yet wired in this commit.
"""

from __future__ import annotations

from hermes_databricks.config import Config


class Lakebase:  # pragma: no cover - Phase 1 stub
    def __init__(self, instance_name: str) -> None:
        self.instance_name = instance_name

    @classmethod
    def from_config(cls, cfg: Config) -> "Lakebase":
        return cls(cfg.lakebase_instance)

    def cursor(self):
        raise NotImplementedError("Lakebase connection helper is implemented in Phase 4")

    def close(self) -> None:
        return None
