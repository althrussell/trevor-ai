"""Terminal tool backends.

Three modes, selectable via ``HERMES_DATABRICKS_TERMINAL_BACKEND``:

* ``in_app_subprocess`` (default) — runs the command via Python's
  ``subprocess.run`` inside the Databricks App container. The working
  directory is restricted to ``<HERMES_HOME>/workspace`` (created if
  missing). Command timeout defaults to 60s, hard-capped at 600s.
  Output is captured and truncated at 64KB per stream. ``shell=False``
  is enforced — commands are always passed as ``list[str]``.

* ``databricks_job`` — placeholder hook that delegates to a Databricks
  Job (configured separately via ``resources/jobs.yml``). Returns a
  ``BackendUnavailable`` diagnostic until wired.

* ``external_sandbox`` — placeholder for Modal/Daytona/Vercel/SSH
  sandboxes. Returns a diagnostic.

* ``disabled`` — every invocation returns the disabled diagnostic.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


log = logging.getLogger("hermes_databricks.tools.terminal_backend")


# Hard limits — Hermes' terminal tool can specify smaller bounds but
# never larger ones here.
HARD_TIMEOUT_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 60
HARD_OUTPUT_BYTES = 64 * 1024  # per stream

# Commands we never let the agent run, even via the in-app subprocess
# backend. Mostly a sanity floor; the broader guard is the cwd restriction.
COMMAND_DENYLIST = {
    "rm", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "passwd", "useradd", "userdel", "groupadd", "iptables",
}


class BackendUnavailable(Exception):
    pass


@dataclass
class TerminalResult:
    ok: bool
    backend: str
    cmd: List[str]
    cwd: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "cmd": self.cmd,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated_stdout": self.truncated_stdout,
            "truncated_stderr": self.truncated_stderr,
            "note": self.note,
        }


def workspace_dir(hermes_home: Path) -> Path:
    ws = Path(hermes_home) / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws.resolve()


def _validate_cwd(workspace: Path, requested_cwd: Optional[str]) -> Path:
    """Ensure ``requested_cwd`` lives under ``workspace``."""
    if not requested_cwd:
        return workspace
    candidate = (workspace / requested_cwd).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"cwd escapes workspace: {requested_cwd!r}") from exc
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def run(
    cmd: List[str],
    *,
    backend: str = "in_app_subprocess",
    hermes_home: Optional[Path] = None,
    cwd: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Optional[dict] = None,
) -> TerminalResult:
    """Execute ``cmd`` (list of arguments) via the selected backend."""
    if not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
        raise ValueError("cmd must be a list[str]; shell strings are not allowed")
    if not cmd:
        raise ValueError("cmd must not be empty")

    if backend == "disabled":
        return TerminalResult(
            ok=False, backend=backend, cmd=cmd, cwd="", exit_code=None,
            stdout="", stderr="",
            note="terminal backend is disabled (HERMES_DATABRICKS_TERMINAL_BACKEND=disabled)",
        )

    if backend == "in_app_subprocess":
        return _run_in_app(cmd, hermes_home=hermes_home, cwd=cwd, timeout=timeout, env=env)

    if backend == "databricks_job":
        return TerminalResult(
            ok=False, backend=backend, cmd=cmd, cwd="", exit_code=None,
            stdout="", stderr="",
            note=(
                "databricks_job backend is not yet wired. Configure a job "
                "in resources/jobs.yml that accepts a 'cmd' parameter and "
                "add the dispatch shim."
            ),
        )

    if backend == "external_sandbox":
        return TerminalResult(
            ok=False, backend=backend, cmd=cmd, cwd="", exit_code=None,
            stdout="", stderr="",
            note=(
                "external_sandbox backend requires explicit configuration "
                "(MODAL_TOKEN_*, DAYTONA_*, VERCEL_*). Set HERMES_DATABRICKS_"
                "TERMINAL_BACKEND back to in_app_subprocess or finish wiring."
            ),
        )

    raise BackendUnavailable(f"unknown terminal backend: {backend!r}")


def _run_in_app(
    cmd: List[str],
    *,
    hermes_home: Optional[Path],
    cwd: Optional[str],
    timeout: float,
    env: Optional[dict],
) -> TerminalResult:
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or "/tmp/hermes_cache/hermes_home").resolve()
    workspace = workspace_dir(home)
    try:
        final_cwd = _validate_cwd(workspace, cwd)
    except ValueError as exc:
        return TerminalResult(
            ok=False, backend="in_app_subprocess", cmd=cmd, cwd="", exit_code=None,
            stdout="", stderr="", note=str(exc),
        )

    timeout = max(1.0, min(float(timeout or DEFAULT_TIMEOUT_SECONDS), HARD_TIMEOUT_SECONDS))

    head = Path(cmd[0]).name.lower()
    if head in COMMAND_DENYLIST:
        return TerminalResult(
            ok=False, backend="in_app_subprocess", cmd=cmd, cwd=str(final_cwd),
            exit_code=None, stdout="", stderr="",
            note=f"command '{head}' is in the in-app denylist",
        )

    # Ensure the executable resolves; reject unknown commands to surface
    # misconfiguration clearly instead of letting subprocess.run raise.
    if "/" not in cmd[0] and shutil.which(cmd[0]) is None:
        return TerminalResult(
            ok=False, backend="in_app_subprocess", cmd=cmd, cwd=str(final_cwd),
            exit_code=None, stdout="", stderr="",
            note=f"executable not found on PATH: {cmd[0]!r}",
        )

    safe_env = dict(env) if env is not None else os.environ.copy()
    safe_env.setdefault("HOME", str(home))
    safe_env.setdefault("PWD", str(final_cwd))

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(final_cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            env=safe_env,
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        trunc_out = len(stdout) > HARD_OUTPUT_BYTES
        trunc_err = len(stderr) > HARD_OUTPUT_BYTES
        if trunc_out:
            stdout = stdout[:HARD_OUTPUT_BYTES] + f"\n[truncated to {HARD_OUTPUT_BYTES} bytes]"
        if trunc_err:
            stderr = stderr[:HARD_OUTPUT_BYTES] + f"\n[truncated to {HARD_OUTPUT_BYTES} bytes]"
        return TerminalResult(
            ok=proc.returncode == 0,
            backend="in_app_subprocess",
            cmd=cmd,
            cwd=str(final_cwd),
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated_stdout=trunc_out,
            truncated_stderr=trunc_err,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("terminal command timed out", extra={"extras": {"cmd": cmd, "timeout": timeout}})
        return TerminalResult(
            ok=False, backend="in_app_subprocess", cmd=cmd, cwd=str(final_cwd),
            exit_code=None,
            stdout=(exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=(exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            note=f"timeout after {timeout}s",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("terminal command raised")
        return TerminalResult(
            ok=False, backend="in_app_subprocess", cmd=cmd, cwd=str(final_cwd),
            exit_code=None, stdout="", stderr="",
            note=f"{type(exc).__name__}: {exc}",
        )
