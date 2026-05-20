"""Lightweight tracing helpers.

For Phase 9 we emit structured spans into `agent_events`. We do not
hard-depend on MLflow Tracing; if it is available we'll also emit a
``mlflow.start_span`` so traces show up in the workspace tracing UI.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger("trevor_databricks.observability.tracing")


@contextlib.contextmanager
def span(
    name: str,
    *,
    attrs: dict[str, Any] | None = None,
    sink: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Time a block and report start/stop to ``sink`` if provided.

    Yields a mutable dict the caller can write attributes to; on exit
    the final dict is passed to ``sink``. Optional MLflow span is
    started/closed alongside; failures are swallowed.
    """
    span_id = uuid.uuid4().hex
    started = time.perf_counter()
    payload: dict[str, Any] = {
        "span_id": span_id,
        "name": name,
        "started_at": time.time(),
        "attrs": dict(attrs or {}),
        "status": "ok",
    }

    ml = None
    try:
        import mlflow  # type: ignore

        ml = mlflow.start_span(name)
        ml.__enter__()
    except Exception:
        ml = None

    try:
        yield payload
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        if ml is not None:
            with contextlib.suppress(Exception):
                ml.__exit__(None, None, None)
        if sink is not None:
            try:
                sink(payload)
            except Exception:
                log.exception("tracing sink failed", extra={"extras": {"span": payload}})
