"""UC Volume-backed HERMES_HOME mirror.

Databricks Apps do **not** mount UC Volumes as a POSIX filesystem. All
I/O has to go through the Databricks SDK Files API. To preserve
Hermes' assumption that ``HERMES_HOME`` is a regular directory, we run
a two-layer model:

* The **local cache** lives under ``cfg.trevor_home`` (default
  ``/tmp/trevor_cache/trevor_home``). Hermes reads and writes here
  normally — the cache is a real on-disk directory tree.

* The **durable mirror** lives at
  ``/Volumes/{catalog}/{schema}/{trevor_home_volume}``. The runtime
  performs:
    * ``sync_from_volume()`` on boot to hydrate the cache.
    * ``sync_to_volume()`` periodically (driven by the supervisor's
      heartbeat) and on shutdown.

Only specific subpaths are considered durable. Ephemeral subpaths
(``state.db`` for example — Lakebase is the source of truth) are
**excluded** from upload but still readable in the cache.

Security:

* All paths are normalised relative to ``HERMES_HOME``.
* Any attempt to escape (``..``, absolute path, drive letter, mixed
  separators) raises ``ValueError``.
* Volume-side paths are constrained to the configured volume prefix.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trevor_databricks.config import Config

log = logging.getLogger("trevor_databricks.fs.volume_fs")


# Subpaths whose contents the supervisor will upload after a write
# and during periodic sync. Order matters only for clarity.
DEFAULT_DURABLE_SUBPATHS: tuple = (
    "skills",
    "optional-skills",
    "cron",
    "memories",
    "image_cache",
    "audio_cache",
    "mcp-tokens",
    "hooks",
    "pairing",
    "logs/curator",
    "config.yaml",
    ".env",
    "soul.md",
    "MEMORY.md",
    "processes.json",
)

# Subpaths we deliberately never upload (large, transient, or
# represented elsewhere — e.g. state.db is replaced by Lakebase).
EXCLUDE_FROM_UPLOAD: tuple = (
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "kanban.db",
    "tmp",
    "sessions",  # informational JSON snapshots, but high churn
    "logs/agent.log",
    "logs/errors.log",
    "logs/gateway.log",
)

# Max file size we'll mirror back to UC Volume (in MB). Avoids
# accidentally uploading huge model caches.
DEFAULT_MAX_FILE_MB = 10


@dataclass
class FileEntry:
    name: str
    relative_path: str
    is_dir: bool
    size: int | None = None
    modified_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "is_dir": self.is_dir,
            "size": self.size,
            "modified_at": self.modified_at,
        }


class UCVolumeHome:
    """Local cache + UC Volume mirror for HERMES_HOME."""

    def __init__(
        self,
        local_root: Path,
        volume_root: str,
        *,
        durable_subpaths: Iterable[str] = DEFAULT_DURABLE_SUBPATHS,
        exclude: Iterable[str] = EXCLUDE_FROM_UPLOAD,
        max_file_mb: int = DEFAULT_MAX_FILE_MB,
        workspace_factory=None,
    ) -> None:
        self.local_root = Path(local_root).resolve()
        if not volume_root.startswith("/Volumes/"):
            raise ValueError(f"volume_root must start with /Volumes/, got {volume_root!r}")
        self.volume_root = volume_root.rstrip("/")
        self.durable_subpaths: set[str] = {s.strip("/") for s in durable_subpaths if s}
        self.exclude: set[str] = {s.strip("/") for s in exclude if s}
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self._workspace_factory = workspace_factory
        self._workspace: Any = None
        self._lock = threading.RLock()
        self._last_sync_from_volume: float | None = None
        self._last_sync_to_volume: float | None = None
        self._files_uploaded: int = 0
        self._files_downloaded: int = 0
        self._bytes_uploaded: int = 0
        self._bytes_downloaded: int = 0
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Config, *, workspace_factory=None) -> UCVolumeHome:
        return cls(
            local_root=cfg.trevor_home,
            volume_root=cfg.trevor_home_volume_path,
            workspace_factory=workspace_factory,
        )

    # ------------------------------------------------------------------
    # Local setup
    # ------------------------------------------------------------------

    def ensure_local_dirs(self) -> None:
        self.local_root.mkdir(parents=True, exist_ok=True)
        for sub in self.durable_subpaths:
            # Treat anything that doesn't look like a file as a dir.
            if "." not in Path(sub).name or sub.endswith("/"):
                (self.local_root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path guards
    # ------------------------------------------------------------------

    def _resolve_local(self, rel: str) -> Path:
        """Resolve a relative path under the cache; reject escapes and absolutes."""
        if rel is None:
            raise ValueError("path is required")
        rel_str = str(rel).strip()
        if not rel_str:
            return self.local_root
        # Reject any explicit absolute path (POSIX or Windows drive letters).
        # We do NOT silently strip leading slashes — that's confusing.
        if rel_str.startswith(("/", "\\")) or (len(rel_str) > 1 and rel_str[1] == ":"):
            raise ValueError(f"Absolute paths are not allowed under HERMES_HOME: {rel!r}")
        # Normalise separators; reject ``..`` and any post-resolution escape.
        normalised = rel_str.replace("\\", "/")
        parts = [p for p in normalised.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"Path escapes HERMES_HOME: {rel!r}")
        candidate = (self.local_root / Path(*parts)).resolve()
        try:
            candidate.relative_to(self.local_root)
        except ValueError as exc:
            raise ValueError(f"Path escapes HERMES_HOME: {rel!r}") from exc
        return candidate

    def _resolve_volume(self, rel: str) -> str:
        rel_str = str(rel).strip()
        if not rel_str:
            return self.volume_root
        if rel_str.startswith(("/", "\\")) or (len(rel_str) > 1 and rel_str[1] == ":"):
            raise ValueError(f"Absolute paths are not allowed: {rel!r}")
        normalised = rel_str.replace("\\", "/")
        parts = [p for p in normalised.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"Path escapes volume root: {rel!r}")
        return self.volume_root + "/" + "/".join(parts)

    def _is_excluded(self, rel: str) -> bool:
        rel_norm = rel.strip("/")
        if not rel_norm:
            return False
        return any(rel_norm == excl or rel_norm.startswith(excl + "/") for excl in self.exclude)

    def _is_durable(self, rel: str) -> bool:
        rel_norm = rel.strip("/")
        if not rel_norm:
            return False
        for subp in self.durable_subpaths:
            if rel_norm == subp or rel_norm.startswith(subp + "/"):
                return True
        return False

    # ------------------------------------------------------------------
    # Basic file ops (cache + push)
    # ------------------------------------------------------------------

    def read_text(self, rel: str) -> str:
        return self._resolve_local(rel).read_text(encoding="utf-8")

    def read_bytes(self, rel: str) -> bytes:
        return self._resolve_local(rel).read_bytes()

    def exists(self, rel: str) -> bool:
        return self._resolve_local(rel).exists()

    def write_text(self, rel: str, content: str, *, push: bool = True) -> None:
        path = self._resolve_local(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if push and self._is_durable(rel):
            try:
                self._upload_one(rel)
            except Exception:
                log.exception("push-on-write failed for %s", rel)

    def write_bytes(self, rel: str, data: bytes, *, push: bool = True) -> None:
        path = self._resolve_local(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if push and self._is_durable(rel):
            try:
                self._upload_one(rel)
            except Exception:
                log.exception("push-on-write failed for %s", rel)

    def delete(self, rel: str, *, also_volume: bool = True) -> None:
        path = self._resolve_local(rel)
        if path.exists():
            if path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink(missing_ok=True)
                    else:
                        with contextlib.suppress(OSError):
                            child.rmdir()
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        if also_volume:
            try:
                w = self._client()
                w.files.delete(self._resolve_volume(rel))
            except Exception as exc:
                log.debug("Volume delete swallowed for %s: %s", rel, exc)

    def list(self, rel: str = "") -> builtins.list[dict[str, Any]]:
        local_dir = self._resolve_local(rel)
        if not local_dir.exists():
            return []
        out: list[FileEntry] = []
        for entry in sorted(local_dir.iterdir()):
            stat = entry.stat()
            relative = str(entry.relative_to(self.local_root))
            out.append(
                FileEntry(
                    name=entry.name,
                    relative_path=relative,
                    is_dir=entry.is_dir(),
                    size=stat.st_size if entry.is_file() else None,
                    modified_at=stat.st_mtime,
                )
            )
        return [e.to_dict() for e in out]

    # ------------------------------------------------------------------
    # First-boot seeding
    # ------------------------------------------------------------------

    def seed_if_empty(
        self,
        seed_dir: Path,
        target_subpath: str,
        *,
        push: bool = True,
    ) -> dict[str, Any]:
        """Copy ``seed_dir`` into ``<HERMES_HOME>/<target_subpath>`` when empty.

        Used to seed the bundled Databricks skills (and any other
        first-boot defaults we ship in ``app/seeds/``) into the durable
        UC Volume mirror on the very first boot of a new App. The
        target is considered "empty" if the local cache directory does
        not exist or contains no files. Subsequent boots skip the seed
        because ``sync_from_volume()`` will have already hydrated the
        cache from the durable mirror.

        Returns a dict describing what happened so the caller can log
        it on the supervisor's ``agent_events`` ledger.
        """
        seed_dir = Path(seed_dir)
        result: dict[str, Any] = {
            "target_subpath": target_subpath,
            "seed_dir": str(seed_dir),
            "seeded": False,
            "files_copied": 0,
            "bytes_copied": 0,
            "skipped_reason": None,
        }

        if not seed_dir.exists() or not seed_dir.is_dir():
            result["skipped_reason"] = "seed_dir_missing"
            return result

        try:
            local_target = self._resolve_local(target_subpath)
        except ValueError as exc:
            result["skipped_reason"] = f"invalid_target: {exc}"
            return result

        if local_target.exists() and any(local_target.rglob("*")):
            result["skipped_reason"] = "target_not_empty"
            return result

        with self._lock:
            local_target.mkdir(parents=True, exist_ok=True)
            for src_file in seed_dir.rglob("*"):
                if not src_file.is_file():
                    continue
                rel_from_seed = src_file.relative_to(seed_dir)
                dest = local_target / rel_from_seed
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    data = src_file.read_bytes()
                except OSError as exc:
                    log.warning("seed_if_empty: read failed for %s: %s", src_file, exc)
                    continue
                dest.write_bytes(data)
                result["files_copied"] += 1
                result["bytes_copied"] += len(data)

            result["seeded"] = True
            log.info(
                "seeded HERMES_HOME subpath",
                extra={
                    "extras": {
                        "target_subpath": target_subpath,
                        "files_copied": result["files_copied"],
                        "bytes_copied": result["bytes_copied"],
                    }
                },
            )

            if push and self._is_durable(target_subpath):
                try:
                    push_result = self.touch_subpath(target_subpath)
                    result["pushed"] = push_result
                except Exception:
                    log.exception("seed_if_empty: push to volume failed for %s", target_subpath)
                    result["pushed"] = {"ok": False, "reason": "push_failed"}

        return result

    def seed_file_if_missing(
        self,
        seed_file: Path,
        target_subpath: str,
        *,
        push: bool = True,
    ) -> dict[str, Any]:
        """Copy ``seed_file`` into ``<HERMES_HOME>/<target_subpath>`` if missing.

        Companion to :meth:`seed_if_empty` for single files (e.g.
        ``soul.md``, ``config.yaml``) that don't fit the per-skill
        directory pattern. Idempotent: a second call is a no-op once
        the destination exists.

        For files that ship with the bundle and should always reflect
        the bundle's copy (the system prompt is one — operators don't
        edit it live), use :meth:`seed_file_force` instead.
        """
        seed_file = Path(seed_file)
        result: dict[str, Any] = {
            "target_subpath": target_subpath,
            "seed_file": str(seed_file),
            "seeded": False,
            "bytes_copied": 0,
            "skipped_reason": None,
        }

        if not seed_file.exists() or not seed_file.is_file():
            result["skipped_reason"] = "seed_file_missing"
            return result

        try:
            local_target = self._resolve_local(target_subpath)
        except ValueError as exc:
            result["skipped_reason"] = f"invalid_target: {exc}"
            return result

        if local_target.exists():
            result["skipped_reason"] = "target_exists"
            return result

        with self._lock:
            local_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = seed_file.read_bytes()
            except OSError as exc:
                result["skipped_reason"] = f"read_failed: {exc}"
                return result
            local_target.write_bytes(data)
            result["seeded"] = True
            result["bytes_copied"] = len(data)
            log.info(
                "seeded HERMES_HOME file",
                extra={
                    "extras": {
                        "target_subpath": target_subpath,
                        "bytes_copied": result["bytes_copied"],
                    }
                },
            )

            if push and self._is_durable(target_subpath):
                try:
                    push_result = self.touch_subpath(target_subpath)
                    result["pushed"] = push_result
                except Exception:
                    log.exception(
                        "seed_file_if_missing: push to volume failed for %s", target_subpath
                    )
                    result["pushed"] = {"ok": False, "reason": "push_failed"}

        return result

    def seed_file_force(
        self,
        seed_file: Path,
        target_subpath: str,
        *,
        push: bool = True,
    ) -> dict[str, Any]:
        """Copy ``seed_file`` into ``<HERMES_HOME>/<target_subpath>``, overwriting.

        Used for files that ship with the bundle and must always reflect
        the bundled copy after a redeploy — primarily ``soul.md`` (the
        system prompt). Without this, ``seed_file_if_missing`` short-
        circuits on every boot after the first and stale prompts persist
        in the UC volume mirror across deploys.

        Returns the same shape as :meth:`seed_file_if_missing`. The
        ``overwritten`` field is ``True`` if the target already existed
        and was replaced (the common case after the first deploy); the
        ``bytes_changed`` field reports whether the on-disk content was
        actually different — useful for surfacing meaningful boot-time
        diagnostics ("nothing to do" vs. "soul prompt updated").
        """
        seed_file = Path(seed_file)
        result: dict[str, Any] = {
            "target_subpath": target_subpath,
            "seed_file": str(seed_file),
            "seeded": False,
            "overwritten": False,
            "bytes_changed": False,
            "bytes_copied": 0,
            "skipped_reason": None,
        }

        if not seed_file.exists() or not seed_file.is_file():
            result["skipped_reason"] = "seed_file_missing"
            return result

        try:
            local_target = self._resolve_local(target_subpath)
        except ValueError as exc:
            result["skipped_reason"] = f"invalid_target: {exc}"
            return result

        with self._lock:
            local_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = seed_file.read_bytes()
            except OSError as exc:
                result["skipped_reason"] = f"read_failed: {exc}"
                return result

            previous_data: bytes | None = None
            if local_target.exists() and local_target.is_file():
                try:
                    previous_data = local_target.read_bytes()
                except OSError:
                    previous_data = None
                result["overwritten"] = True

            if previous_data == data:
                result["seeded"] = True
                result["bytes_copied"] = len(data)
                result["bytes_changed"] = False
                # Identical content; skip the disk write and the push,
                # but still report success so the caller can log it.
                return result

            local_target.write_bytes(data)
            result["seeded"] = True
            result["bytes_copied"] = len(data)
            result["bytes_changed"] = True
            log.info(
                "force-seeded HERMES_HOME file (bundle copy wins)",
                extra={
                    "extras": {
                        "target_subpath": target_subpath,
                        "overwritten": result["overwritten"],
                        "bytes_copied": result["bytes_copied"],
                    }
                },
            )

            if push and self._is_durable(target_subpath):
                try:
                    push_result = self.touch_subpath(target_subpath)
                    result["pushed"] = push_result
                except Exception:
                    log.exception("seed_file_force: push to volume failed for %s", target_subpath)
                    result["pushed"] = {"ok": False, "reason": "push_failed"}

        return result

    # ------------------------------------------------------------------
    # Sync — volume → local
    # ------------------------------------------------------------------

    def sync_from_volume(self) -> dict[str, Any]:
        with self._lock:
            self._files_downloaded = 0
            self._bytes_downloaded = 0
            try:
                self._walk_volume_into_local(self.volume_root)
            except _NotFound:
                log.info(
                    "UC Volume %s does not exist yet — skipping initial sync", self.volume_root
                )
            self._last_sync_from_volume = time.time()
            return {
                "files_downloaded": self._files_downloaded,
                "bytes_downloaded": self._bytes_downloaded,
                "at": self._last_sync_from_volume,
            }

    def _walk_volume_into_local(self, vol_path: str) -> None:
        w = self._client()
        try:
            entries = list(w.files.list_directory_contents(vol_path))
        except Exception as exc:
            if _is_not_found(exc):
                raise _NotFound() from exc
            log.warning("list_directory_contents failed for %s: %s", vol_path, exc)
            return

        for entry in entries:
            path = getattr(entry, "path", None) or getattr(entry, "name", None)
            if not path:
                continue
            is_dir = bool(getattr(entry, "is_directory", False))
            if is_dir:
                self._walk_volume_into_local(path)
                continue
            rel = self._volume_to_relative(path)
            if rel is None:
                continue
            try:
                local_path = self._resolve_local(rel)
            except ValueError:
                continue
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                resp = w.files.download(path)
                contents = resp.contents.read() if hasattr(resp.contents, "read") else resp.contents
                local_path.write_bytes(contents)
                self._files_downloaded += 1
                self._bytes_downloaded += len(contents)
            except Exception:
                log.exception("download failed for %s", path)
                self._errors.append(f"download:{path}")

    def _volume_to_relative(self, vol_path: str) -> str | None:
        if not vol_path.startswith(self.volume_root):
            return None
        rel = vol_path[len(self.volume_root) :].lstrip("/")
        return rel or None

    # ------------------------------------------------------------------
    # Sync — local → volume
    # ------------------------------------------------------------------

    def sync_to_volume(self, *, only_durable: bool = True) -> dict[str, Any]:
        with self._lock:
            self._files_uploaded = 0
            self._bytes_uploaded = 0
            roots: Iterable[Path]
            if only_durable:
                roots = self._durable_local_paths()
            else:
                roots = [self.local_root]
            for root in roots:
                if not root.exists():
                    continue
                if root.is_file():
                    self._upload_path(root)
                else:
                    for entry in root.rglob("*"):
                        if entry.is_file():
                            self._upload_path(entry)
            self._last_sync_to_volume = time.time()
            return {
                "files_uploaded": self._files_uploaded,
                "bytes_uploaded": self._bytes_uploaded,
                "at": self._last_sync_to_volume,
            }

    def _durable_local_paths(self) -> builtins.list[Path]:
        out: list[Path] = []
        for sub in self.durable_subpaths:
            out.append(self.local_root / sub)
        return out

    def touch_subpath(self, subpath: str) -> dict[str, Any]:
        """Upload everything under ``subpath`` (relative to HERMES_HOME) now.

        Used by the cron scheduler after every tick to push
        ``cron/jobs.json`` (and its ``output/`` siblings) to the
        durable volume mirror — otherwise the next App restart would
        forget ``next_run_at`` advances and re-run jobs.
        """
        try:
            local = self._resolve_local(subpath)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        uploaded = 0
        bytes_uploaded = 0
        if local.exists():
            if local.is_file():
                try:
                    self._upload_one(subpath)
                    uploaded += 1
                    bytes_uploaded += local.stat().st_size
                except Exception:
                    log.exception("touch_subpath upload failed for %s", subpath)
            else:
                for entry in local.rglob("*"):
                    if entry.is_file():
                        try:
                            rel = str(entry.relative_to(self.local_root))
                            self._upload_one(rel)
                            uploaded += 1
                            bytes_uploaded += entry.stat().st_size
                        except Exception:
                            log.exception("touch_subpath upload failed for %s", entry)
        return {"ok": True, "uploaded": uploaded, "bytes": bytes_uploaded}

    def _upload_path(self, local_file: Path) -> None:
        rel = str(local_file.relative_to(self.local_root))
        if self._is_excluded(rel):
            return
        try:
            size = local_file.stat().st_size
        except OSError:
            return
        if size > self.max_file_bytes:
            log.debug("Skipping %s (size %d > max %d)", rel, size, self.max_file_bytes)
            return
        try:
            self._upload_one(rel)
        except Exception:
            log.exception("upload failed for %s", rel)
            self._errors.append(f"upload:{rel}")

    def _upload_one(self, rel: str) -> None:
        local_path = self._resolve_local(rel)
        if not local_path.exists() or local_path.is_dir():
            return
        if self._is_excluded(rel):
            return
        vol_path = self._resolve_volume(rel)
        w = self._client()
        data = local_path.read_bytes()
        w.files.upload(vol_path, contents=io.BytesIO(data), overwrite=True)
        self._files_uploaded += 1
        self._bytes_uploaded += len(data)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "local_root": str(self.local_root),
            "volume_root": self.volume_root,
            "durable_subpaths": sorted(self.durable_subpaths),
            "excluded_from_upload": sorted(self.exclude),
            "max_file_bytes": self.max_file_bytes,
            "last_sync_from_volume": self._last_sync_from_volume,
            "last_sync_to_volume": self._last_sync_to_volume,
            "files_uploaded_running": self._files_uploaded,
            "files_downloaded_running": self._files_downloaded,
            "errors_running": list(self._errors[-25:]),
        }

    async def health_probe(self) -> dict[str, Any]:
        try:
            w = self._client()
            # Cheapest reach: list the volume root. Tolerate NotFound (first deploy).
            try:
                _ = list(w.files.list_directory_contents(self.volume_root))
                status = "ok"
            except Exception as exc:
                if _is_not_found(exc):
                    status = "empty"
                else:
                    raise
            return {
                "status": status,
                "volume_root": self.volume_root,
                "last_sync_from_volume": self._last_sync_from_volume,
                "last_sync_to_volume": self._last_sync_to_volume,
            }
        except Exception as exc:
            return {
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
                "volume_root": self.volume_root,
            }

    def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Workspace client
    # ------------------------------------------------------------------

    def _client(self):
        if self._workspace is not None:
            return self._workspace
        if self._workspace_factory is not None:
            self._workspace = self._workspace_factory()
            return self._workspace
        from databricks.sdk import WorkspaceClient  # type: ignore

        self._workspace = WorkspaceClient()
        return self._workspace


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class _NotFound(Exception):
    pass


def _is_not_found(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {"ResourceDoesNotExist", "NotFound", "_NotFound"}:
        return True
    msg = str(exc).lower()
    return "not found" in msg or "does not exist" in msg or "404" in msg
