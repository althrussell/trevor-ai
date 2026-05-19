"""Lakebase-backed Hermes ``SessionDB`` replacement.

This is the **durable source of truth** for Hermes session state when
running on Databricks. It is a duck-typed replacement for
``hermes_state.SessionDB`` — it implements the subset of methods that
``run_conversation`` and ``agent.agent_runtime_helpers`` actually call,
plus a few helpers our own ``/debug/*`` endpoints want.

The schema is defined in ``schema.sql`` and applied by the bundle's
``setup_lakebase`` job; ``ensure_schema()`` will also self-bootstrap
if needed.

Differences vs. the upstream SQLite ``SessionDB``:

* ``search_messages`` / ``search_sessions`` use Postgres ILIKE with a
  ``pg_trgm`` GIN index. The upstream FTS5 with trigram tokenizer is
  not available here; results are best-effort and documented in
  ``docs/RUNTIME_CONSTRAINTS.md``.
* Telegram topic-mode helpers are stubbed (return ``False``/``None``)
  — the core loop does not depend on them and our Telegram channel
  uses flat ``telegram:<chat_id>`` session ids.
* Handoff helpers are stubbed similarly.

Public surface (high-confidence Hermes call sites):

  __init__, close, ensure_schema,
  create_session, ensure_session, get_session, resolve_session_id,
  update_system_prompt, update_token_counts,
  append_message, replace_messages, get_messages, clear_messages,
  get_messages_around, get_anchored_view, get_messages_as_conversation,
  resolve_resume_session_id,
  set_session_title, get_session_title, get_session_by_title,
  resolve_session_by_title, get_next_title_in_lineage,
  list_sessions_rich, session_count, message_count,
  export_session, export_all, delete_session, prune_sessions,
  prune_empty_ghost_sessions, finalize_orphaned_compression_sessions,
  end_session, reopen_session,
  search_messages, search_sessions,
  get_meta, set_meta, vacuum, maybe_auto_prune_and_vacuum,
  get_compression_tip, sanitize_title (static),
  # extras (not in SQLite SessionDB; ours):
  append_event, recent_events, recent_usage, health_probe
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_databricks.config import Config
from hermes_databricks.lakebase import Lakebase


log = logging.getLogger("hermes_databricks.state.lakebase_session_db")

SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def _to_json(value: Any) -> Optional[str]:
    """Serialise a Python value to a JSON string for JSONB columns.

    None → None (NULL). Already-string values are passed through if
    they parse as JSON; otherwise they're wrapped as a JSON string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return json.dumps(value)
    return json.dumps(value, default=str)


def _parse_json(value: Any) -> Any:
    """psycopg returns JSONB as Python dict/list by default; pass through."""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return json.loads(value.decode("utf-8"))
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


# Whitelist of session row columns we read/write.
_SESSION_COLS = (
    "id", "source", "user_id", "model", "model_config", "system_prompt",
    "parent_session_id", "started_at", "ended_at", "end_reason",
    "message_count", "tool_call_count", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "billing_provider", "billing_base_url", "billing_mode",
    "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
    "pricing_version", "title", "api_call_count",
    "handoff_state", "handoff_platform", "handoff_error",
)

_MESSAGE_COLS = (
    "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
    "tool_name", "timestamp", "token_count", "finish_reason",
    "reasoning", "reasoning_content", "reasoning_details",
    "codex_reasoning_items", "codex_message_items",
)


def _row_to_dict(cols: Iterable[str], row: Optional[tuple]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out: Dict[str, Any] = {}
    for c, v in zip(cols, row):
        if c in {
            "model_config", "tool_calls", "reasoning_details",
            "codex_reasoning_items", "codex_message_items",
            "handoff_state",
        }:
            out[c] = _parse_json(v)
        elif hasattr(v, "isoformat"):
            out[c] = v.isoformat()
        else:
            out[c] = v
    return out


class LakebaseSessionDB:
    """Duck-typed ``SessionDB`` backed by Lakebase."""

    SCHEMA_VERSION = 11  # informational; matches the upstream SQLite schema

    def __init__(self, lakebase: Lakebase, *, schema: str = "hermes_session") -> None:
        self.lakebase = lakebase
        self.schema = schema
        self._schema_applied = False
        self._search_path_set = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Config, *, lakebase: Optional[Lakebase] = None) -> "LakebaseSessionDB":
        lakebase = lakebase or Lakebase.from_config(cfg)
        return cls(lakebase=lakebase, schema=cfg.lakebase_schema)

    def close(self) -> None:
        self.lakebase.close()

    def ensure_schema(self) -> None:
        """Apply ``schema.sql`` once per process.

        Idempotent. We tolerate ``permission denied`` on ``CREATE
        EXTENSION`` and ``CREATE SCHEMA`` so that the App service
        principal (which typically lacks superuser privileges) can
        still bring the session DB online when the setup_lakebase job
        has already created the schema + extension as the workspace
        user.
        """
        if self._schema_applied:
            return
        sql_text = SCHEMA_FILE.read_text()
        sql_text = sql_text.replace("hermes_session", self.schema)
        statements = [s.strip() for s in sql_text.split(";") if s.strip() and not s.strip().startswith("--")]

        with self.lakebase.cursor() as cur:
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception as exc:
                    upper = stmt.upper()
                    # Tolerate priv-required DDL the setup_lakebase job owns.
                    if (
                        upper.startswith("CREATE EXTENSION")
                        or upper.startswith("CREATE SCHEMA")
                    ):
                        log.warning(
                            "Skipping privileged DDL (assumed run by setup_lakebase job): %s — %s",
                            stmt.split("\n", 1)[0], exc,
                        )
                        continue
                    raise
        self._schema_applied = True
        self._search_path_set = True
        log.info("Lakebase schema '%s' ensured", self.schema)

    def _ensure_search_path(self, cur) -> None:
        if not self._search_path_set:
            cur.execute(f'SET search_path TO "{self.schema}"')
            self._search_path_set = True

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        source: str,
        *,
        user_id: Optional[str] = None,
        model: Optional[str] = None,
        model_config: Any = None,
        system_prompt: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        title: Optional[str] = None,
        **_ignored,
    ) -> str:
        title = self.sanitize_title(title) if title else None
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                """
                INSERT INTO sessions (
                    id, source, user_id, model, model_config, system_prompt,
                    parent_session_id, billing_provider, billing_base_url,
                    billing_mode, title
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    source = EXCLUDED.source,
                    model = COALESCE(EXCLUDED.model, sessions.model),
                    model_config = COALESCE(EXCLUDED.model_config, sessions.model_config),
                    system_prompt = COALESCE(EXCLUDED.system_prompt, sessions.system_prompt),
                    parent_session_id = COALESCE(EXCLUDED.parent_session_id, sessions.parent_session_id),
                    title = COALESCE(EXCLUDED.title, sessions.title)
                """,
                (
                    session_id, source, user_id, model, _to_json(model_config),
                    system_prompt, parent_session_id, billing_provider,
                    billing_base_url, billing_mode, title,
                ),
            )
        return session_id

    def ensure_session(self, session_id: str, source: str = "unknown", model: Optional[str] = None, **kwargs) -> str:
        existing = self.get_session(session_id)
        if existing is not None:
            return session_id
        return self.create_session(session_id, source, model=model, **kwargs)

    def end_session(self, session_id: str, end_reason: Optional[str] = None) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "UPDATE sessions SET ended_at = now(), end_reason = COALESCE(%s, end_reason) WHERE id = %s",
                (end_reason, session_id),
            )

    def reopen_session(self, session_id: str) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = %s",
                (session_id,),
            )

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "UPDATE sessions SET system_prompt = %s WHERE id = %s",
                (system_prompt, session_id),
            )

    def update_token_counts(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: Optional[str] = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token + cost stats. ``absolute=True`` sets values directly; otherwise increments."""
        if absolute:
            sets = [
                "input_tokens = %s",
                "output_tokens = %s",
                "cache_read_tokens = %s",
                "cache_write_tokens = %s",
                "reasoning_tokens = %s",
                "api_call_count = %s",
            ]
            params: List[Any] = [
                input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, api_call_count,
            ]
        else:
            sets = [
                "input_tokens = COALESCE(input_tokens,0) + %s",
                "output_tokens = COALESCE(output_tokens,0) + %s",
                "cache_read_tokens = COALESCE(cache_read_tokens,0) + %s",
                "cache_write_tokens = COALESCE(cache_write_tokens,0) + %s",
                "reasoning_tokens = COALESCE(reasoning_tokens,0) + %s",
                "api_call_count = COALESCE(api_call_count,0) + %s",
            ]
            params = [
                input_tokens, output_tokens, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, api_call_count,
            ]

        if model is not None:
            sets.append("model = %s")
            params.append(model)
        if estimated_cost_usd is not None:
            sets.append("estimated_cost_usd = %s")
            params.append(estimated_cost_usd)
        if actual_cost_usd is not None:
            sets.append("actual_cost_usd = %s")
            params.append(actual_cost_usd)
        if cost_status is not None:
            sets.append("cost_status = %s")
            params.append(cost_status)
        if cost_source is not None:
            sets.append("cost_source = %s")
            params.append(cost_source)
        if pricing_version is not None:
            sets.append("pricing_version = %s")
            params.append(pricing_version)
        if billing_provider is not None:
            sets.append("billing_provider = %s")
            params.append(billing_provider)
        if billing_base_url is not None:
            sets.append("billing_base_url = %s")
            params.append(billing_base_url)
        if billing_mode is not None:
            sets.append("billing_mode = %s")
            params.append(billing_mode)

        params.append(session_id)
        sql = f"UPDATE sessions SET {', '.join(sets)} WHERE id = %s"

        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(sql, tuple(params))

        # Also append a usage_ledger row for analytics. Best-effort.
        try:
            with self.lakebase.cursor() as cur:
                self._ensure_search_path(cur)
                cur.execute(
                    """
                    INSERT INTO usage_ledger (
                        session_id, provider, model, prompt_tokens, completion_tokens,
                        total_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
                        estimated_cost_usd, raw_usage
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        session_id, billing_provider or "databricks", model,
                        input_tokens, output_tokens,
                        (input_tokens or 0) + (output_tokens or 0),
                        cache_read_tokens, cache_write_tokens, reasoning_tokens,
                        estimated_cost_usd, _to_json({
                            "absolute": absolute,
                            "api_call_count": api_call_count,
                            "cost_status": cost_status,
                            "cost_source": cost_source,
                        }),
                    ),
                )
        except Exception:
            log.debug("usage_ledger insert failed", exc_info=True)

    # ------------------------------------------------------------------
    # Session lookups
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                f"SELECT {', '.join(_SESSION_COLS)} FROM sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
        return _row_to_dict(_SESSION_COLS, row)

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        if not session_id_or_prefix:
            return None
        full = self.get_session(session_id_or_prefix)
        if full is not None:
            return session_id_or_prefix
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "SELECT id FROM sessions WHERE id LIKE %s ORDER BY started_at DESC LIMIT 2",
                (session_id_or_prefix + "%",),
            )
            rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0][0]
        return None  # ambiguous or missing

    def resolve_resume_session_id(self, session_id: str) -> str:
        # In SQLite SessionDB this walks the compression lineage to the
        # latest child. We don't implement compression-lineage yet, so
        # return the canonical id as-is.
        return session_id

    # ------------------------------------------------------------------
    # Titles
    # ------------------------------------------------------------------

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        if title is None:
            return None
        # Mirror the SQLite SessionDB behaviour: strip control chars, cap length.
        cleaned = re.sub(r"[\x00-\x1f\x7f]", "", title).strip()
        if not cleaned:
            return None
        return cleaned[:200]

    def set_session_title(self, session_id: str, title: Optional[str]) -> bool:
        clean = self.sanitize_title(title)
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "UPDATE sessions SET title = %s WHERE id = %s",
                (clean, session_id),
            )
            return cur.rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute("SELECT title FROM sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                f"SELECT {', '.join(_SESSION_COLS)} FROM sessions "
                "WHERE title = %s ORDER BY started_at DESC LIMIT 1",
                (title,),
            )
            row = cur.fetchone()
        return _row_to_dict(_SESSION_COLS, row)

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        row = self.get_session_by_title(title)
        return row["id"] if row else None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "SELECT title FROM sessions WHERE title ILIKE %s ORDER BY title DESC LIMIT 1",
                (base_title + "%",),
            )
            row = cur.fetchone()
        if row is None:
            return base_title
        # Find a trailing digit and bump it.
        m = re.search(r"\s+(\d+)$", row[0] or "")
        if m:
            n = int(m.group(1)) + 1
            return re.sub(r"\s+\d+$", f" {n}", row[0])
        return f"{base_title} 2"

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        # Compression lineage is not yet ported. Returning None matches
        # the SQLite path when no compression has happened.
        return None

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def append_message(
        self,
        session_id: str,
        role: str,
        *,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls: Any = None,
        tool_call_id: Optional[str] = None,
        token_count: Optional[int] = None,
        finish_reason: Optional[str] = None,
        reasoning: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
    ) -> int:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, tool_call_id, tool_calls, tool_name,
                    token_count, finish_reason, reasoning, reasoning_content,
                    reasoning_details, codex_reasoning_items, codex_message_items
                ) VALUES (
                    %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb
                ) RETURNING id
                """,
                (
                    session_id, role, content, tool_call_id, _to_json(tool_calls), tool_name,
                    token_count, finish_reason, reasoning, reasoning_content,
                    _to_json(reasoning_details),
                    _to_json(codex_reasoning_items),
                    _to_json(codex_message_items),
                ),
            )
            row = cur.fetchone()
            msg_id = int(row[0]) if row else 0
            # Bump aggregate counts on the parent session.
            cur.execute(
                "UPDATE sessions SET message_count = COALESCE(message_count,0) + 1 WHERE id = %s",
                (session_id,),
            )
            if tool_calls:
                cur.execute(
                    "UPDATE sessions SET tool_call_count = COALESCE(tool_call_count,0) + %s WHERE id = %s",
                    (_count_tool_calls(tool_calls), session_id),
                )
        return msg_id

    def replace_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute("DELETE FROM messages WHERE session_id = %s", (session_id,))
            cur.execute("UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = %s", (session_id,))
        for msg in messages:
            self.append_message(
                session_id,
                role=msg.get("role", "user"),
                content=msg.get("content"),
                tool_name=msg.get("tool_name"),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
                token_count=msg.get("token_count"),
                finish_reason=msg.get("finish_reason"),
                reasoning=msg.get("reasoning"),
                reasoning_content=msg.get("reasoning_content"),
                reasoning_details=msg.get("reasoning_details"),
                codex_reasoning_items=msg.get("codex_reasoning_items"),
                codex_message_items=msg.get("codex_message_items"),
            )

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                f"SELECT {', '.join(_MESSAGE_COLS)} FROM messages "
                "WHERE session_id = %s ORDER BY id ASC",
                (session_id,),
            )
            rows = cur.fetchall()
        return [d for d in (_row_to_dict(_MESSAGE_COLS, r) for r in rows) if d is not None]

    def get_messages_around(self, session_id: str, anchor_msg_id: int, before: int = 5, after: int = 5) -> List[Dict[str, Any]]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                f"(SELECT {', '.join(_MESSAGE_COLS)} FROM messages WHERE session_id = %s AND id < %s "
                "ORDER BY id DESC LIMIT %s) UNION ALL "
                f"(SELECT {', '.join(_MESSAGE_COLS)} FROM messages WHERE session_id = %s AND id >= %s "
                "ORDER BY id ASC LIMIT %s)",
                (session_id, anchor_msg_id, before, session_id, anchor_msg_id, after + 1),
            )
            rows = cur.fetchall()
        msgs = [d for d in (_row_to_dict(_MESSAGE_COLS, r) for r in rows) if d is not None]
        msgs.sort(key=lambda m: m["id"])
        return msgs

    def get_anchored_view(self, session_id: str, anchor_msg_id: int, before: int = 10, after: int = 10) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "anchor": anchor_msg_id,
            "messages": self.get_messages_around(session_id, anchor_msg_id, before, after),
        }

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """Return messages in the OpenAI chat format (role + content + tool_*)."""
        out: List[Dict[str, Any]] = []
        for m in self.get_messages(session_id):
            entry: Dict[str, Any] = {"role": m["role"]}
            if m.get("content") is not None:
                entry["content"] = m["content"]
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            if m.get("tool_name"):
                entry["name"] = m["tool_name"]
            out.append(entry)
        return out

    def clear_messages(self, session_id: str) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute("DELETE FROM messages WHERE session_id = %s", (session_id,))
            cur.execute("UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = %s", (session_id,))

    # ------------------------------------------------------------------
    # Listing / aggregates
    # ------------------------------------------------------------------

    def list_sessions_rich(
        self,
        source: Optional[str] = None,
        exclude_sources: Optional[Iterable[str]] = None,
        limit: int = 20,
        offset: int = 0,
        *,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []
        if source:
            where.append("source = %s")
            params.append(source)
        if exclude_sources:
            placeholders = ", ".join(["%s"] * len(list(exclude_sources)))
            where.append(f"source NOT IN ({placeholders})")
            params.extend(list(exclude_sources))

        order = "started_at DESC"
        if order_by_last_active:
            order = (
                "(SELECT MAX(timestamp) FROM messages m WHERE m.session_id = sessions.id) DESC NULLS LAST"
            )

        sql = f"SELECT {', '.join(_SESSION_COLS)} FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        out = [d for d in (_row_to_dict(_SESSION_COLS, r) for r in rows) if d is not None]
        if project_compression_tips:
            for row in out:
                row["compression_tip"] = None  # not implemented
        return out

    def session_count(self, source: Optional[str] = None) -> int:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            if source:
                cur.execute("SELECT count(*) FROM sessions WHERE source = %s", (source,))
            else:
                cur.execute("SELECT count(*) FROM sessions")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def message_count(self, session_id: Optional[str] = None) -> int:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            if session_id:
                cur.execute("SELECT count(*) FROM messages WHERE session_id = %s", (session_id,))
            else:
                cur.execute("SELECT count(*) FROM messages")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Search (ILIKE/trgm — best-effort; FTS5 not available on Postgres)
    # ------------------------------------------------------------------

    def search_messages(
        self,
        query: str,
        *,
        source_filter: Optional[str] = None,
        exclude_sources: Optional[Iterable[str]] = None,
        role_filter: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        sort: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not query:
            return []
        like = f"%{query}%"
        where = ["m.content ILIKE %s"]
        params: List[Any] = [like]
        if role_filter:
            where.append("m.role = %s")
            params.append(role_filter)
        if source_filter:
            where.append("s.source = %s")
            params.append(source_filter)
        if exclude_sources:
            placeholders = ", ".join(["%s"] * len(list(exclude_sources)))
            where.append(f"s.source NOT IN ({placeholders})")
            params.extend(list(exclude_sources))

        order = "m.timestamp DESC"
        if sort == "oldest":
            order = "m.timestamp ASC"

        cols = ", ".join([f"m.{c}" for c in _MESSAGE_COLS])
        sql = (
            f"SELECT {cols}, s.source AS session_source "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])

        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        out: List[Dict[str, Any]] = []
        for row in rows:
            d = _row_to_dict(_MESSAGE_COLS, row[:len(_MESSAGE_COLS)])
            if d is None:
                continue
            d["session_source"] = row[-1]
            out.append(d)
        return out

    def search_sessions(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        if not query:
            return []
        like = f"%{query}%"
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                f"SELECT {', '.join(_SESSION_COLS)} FROM sessions "
                "WHERE title ILIKE %s OR system_prompt ILIKE %s OR id ILIKE %s "
                "ORDER BY started_at DESC LIMIT %s",
                (like, like, like, int(kwargs.get("limit", 20))),
            )
            rows = cur.fetchall()
        return [d for d in (_row_to_dict(_SESSION_COLS, r) for r in rows) if d is not None]

    # ------------------------------------------------------------------
    # Export / delete / prune
    # ------------------------------------------------------------------

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.get_session(session_id)
        if session is None:
            return None
        return {"session": session, "messages": self.get_messages(session_id)}

    def export_all(self, source: Optional[str] = None) -> List[Dict[str, Any]]:
        sessions = self.list_sessions_rich(source=source, limit=10_000, project_compression_tips=False)
        return [self.export_session(s["id"]) for s in sessions if s is not None]

    def delete_session(self, session_id: str, sessions_dir: Optional[Path] = None) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))

    def prune_sessions(
        self,
        max_age_days: int,
        max_count: int,
        source: Optional[str] = None,
        sessions_dir: Optional[Path] = None,
    ) -> int:
        deleted = 0
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            params: List[Any] = []
            where = []
            if source:
                where.append("source = %s")
                params.append(source)
            if max_age_days > 0:
                where.append(f"started_at < now() - INTERVAL '{int(max_age_days)} days'")
            sql = "DELETE FROM sessions"
            if where:
                sql += " WHERE " + " AND ".join(where)
            cur.execute(sql, tuple(params))
            deleted += cur.rowcount or 0

            if max_count > 0:
                cur.execute(
                    "DELETE FROM sessions WHERE id IN ("
                    "  SELECT id FROM sessions ORDER BY started_at DESC OFFSET %s"
                    ")",
                    (int(max_count),),
                )
                deleted += cur.rowcount or 0
        return deleted

    def prune_empty_ghost_sessions(self, sessions_dir: Optional[Path] = None) -> int:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session_id FROM messages)"
            )
            return cur.rowcount or 0

    def finalize_orphaned_compression_sessions(self) -> int:
        # Compression lineage isn't ported; nothing to finalise.
        return 0

    # ------------------------------------------------------------------
    # KV / meta
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[Any]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute("SELECT value FROM kv_state WHERE key = %s", (key,))
            row = cur.fetchone()
        return _parse_json(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                """
                INSERT INTO kv_state (key, value, updated_at)
                VALUES (%s, %s::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, _to_json(value)),
            )

    def vacuum(self) -> None:
        # Postgres autovacuum handles this. We could ANALYZE here but it's
        # a no-op for Lakebase semantics.
        return None

    def maybe_auto_prune_and_vacuum(self, *args, **kwargs) -> None:
        return None

    # ------------------------------------------------------------------
    # Telegram topic + handoff helpers (stubs — see RUNTIME_CONSTRAINTS.md)
    # ------------------------------------------------------------------

    def apply_telegram_topic_migration(self) -> None:
        return None

    def enable_telegram_topic_mode(self, *_args, **_kwargs) -> bool:
        return False

    def disable_telegram_topic_mode(self, *_args, **_kwargs) -> bool:
        return False

    def is_telegram_topic_mode_enabled(self, *_args, **_kwargs) -> bool:
        return False

    def get_telegram_topic_binding(self, *_args, **_kwargs) -> Optional[Dict[str, Any]]:
        return None

    def bind_telegram_topic(self, *_args, **_kwargs) -> bool:
        return False

    def is_telegram_session_linked_to_topic(self, *_args, **_kwargs) -> bool:
        return False

    def list_unlinked_telegram_sessions_for_user(self, *_args, **_kwargs) -> List[Dict[str, Any]]:
        return []

    def request_handoff(self, *_args, **_kwargs) -> bool:
        return False

    def get_handoff_state(self, *_args, **_kwargs) -> Optional[Dict[str, Any]]:
        return None

    def list_pending_handoffs(self, *_args, **_kwargs) -> List[Dict[str, Any]]:
        return []

    def claim_handoff(self, *_args, **_kwargs) -> bool:
        return False

    def complete_handoff(self, *_args, **_kwargs) -> bool:
        return False

    def fail_handoff(self, *_args, **_kwargs) -> bool:
        return False

    # ------------------------------------------------------------------
    # Extras we expose for /debug/* endpoints
    # ------------------------------------------------------------------

    def append_event(
        self,
        kind: str,
        payload: Any,
        *,
        channel: Optional[str] = None,
        session_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                """
                INSERT INTO agent_events (id, kind, channel, session_id, thread_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (event_id, kind, channel, session_id, thread_id, _to_json(payload)),
            )
        return event_id

    def recent_events(self, limit: int = 50, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            if kind:
                cur.execute(
                    "SELECT id, ts, kind, channel, session_id, thread_id, payload "
                    "FROM agent_events WHERE kind = %s ORDER BY ts DESC LIMIT %s",
                    (kind, limit),
                )
            else:
                cur.execute(
                    "SELECT id, ts, kind, channel, session_id, thread_id, payload "
                    "FROM agent_events ORDER BY ts DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r[0],
                "ts": r[1].isoformat() if hasattr(r[1], "isoformat") else r[1],
                "kind": r[2],
                "channel": r[3],
                "session_id": r[4],
                "thread_id": r[5],
                "payload": _parse_json(r[6]),
            })
        return out

    def recent_usage(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lakebase.cursor() as cur:
            self._ensure_search_path(cur)
            cur.execute(
                "SELECT id, ts, session_id, provider, model, prompt_tokens, "
                "completion_tokens, total_tokens, estimated_cost_usd "
                "FROM usage_ledger ORDER BY ts DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r[0],
                "ts": r[1].isoformat() if hasattr(r[1], "isoformat") else r[1],
                "session_id": r[2],
                "provider": r[3],
                "model": r[4],
                "prompt_tokens": r[5],
                "completion_tokens": r[6],
                "total_tokens": r[7],
                "estimated_cost_usd": r[8],
            })
        return out

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_probe(self) -> Dict[str, Any]:
        return await self.lakebase.health_probe()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _count_tool_calls(tool_calls: Any) -> int:
    if not tool_calls:
        return 0
    if isinstance(tool_calls, list):
        return len(tool_calls)
    if isinstance(tool_calls, str):
        try:
            parsed = json.loads(tool_calls)
            if isinstance(parsed, list):
                return len(parsed)
        except Exception:
            pass
    return 1
