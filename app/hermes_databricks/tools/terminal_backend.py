"""Terminal tool backend selectors (Phase 1 skeleton; full in Phase 7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TerminalResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: Optional[int]
    backend: str
    note: str = ""


class BackendUnavailable(Exception):
    pass


def run(cmd: list[str], *, backend: str = "in_app_subprocess", cwd: Optional[str] = None, timeout: float = 60.0) -> TerminalResult:  # pragma: no cover - Phase 1 stub
    raise BackendUnavailable("terminal backend not yet wired (Phase 7)")
