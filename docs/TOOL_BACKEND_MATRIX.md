# Tool Backend Matrix

This matrix documents every Hermes tool/toolset we are aware of, what
Hermes uses to execute it locally, and how we route execution inside
the Databricks App container. Tools are **never silently removed** —
if a backend is unavailable, the tool stays registered and returns a
structured "backend required" diagnostic when invoked.

## Status legend

| Status | Meaning |
|--------|---------|
| `works-in-app` | Runs directly inside the App container; no extra config |
| `works-with-uc-volume` | Works if `UCVolumeHome` is bound; uses the cache + sync |
| `works-with-lakebase` | Works via `LakebaseSessionDB` (no SQLite) |
| `requires-job-backend` | Needs the `databricks_job` execution backend (future) |
| `requires-external-sandbox` | Needs Modal/Daytona/Vercel/Browserbase backend |
| `requires-secret` | Works once specific Databricks Secrets are configured |
| `requires-extra` | Works only if a Hermes optional extra is installed |
| `not-yet-validated` | Plumbing exists, end-to-end test is pending |
| `blocked` | Cannot work in the App; alternative documented |

## Toolset summary

| Hermes Toolset | Default in App | Notes |
|----------------|----------------|-------|
| `web` | enabled | HTTP search backends; needs API keys for premium providers |
| `file` | enabled | Routed to `/tmp/hermes_cache` + UC Volume sync |
| `memory` | enabled | Plugin path; default in-cache |
| `skills` | enabled | Uses `HERMES_HOME/skills` mirrored to UC Volume |
| `session` | enabled | `LakebaseSessionDB.search_messages` |
| `cron` | enabled | Jobs in `HERMES_HOME/cron/jobs.json` mirrored to UC Volume |
| `delegation` | enabled | Subprocess `AIAgent` threads inside App |
| `planner` (todo/clarify) | enabled | In-memory + LLM only |
| `terminal` | enabled, restricted | `in_app_subprocess` only by default; cwd + timeout guard |
| `browser` | disabled | Requires `BROWSER_BACKEND` switch + Chromium |
| `computer-use` | disabled | macOS-only cua-driver |
| `mcp` | disabled | Enable `HERMES_DATABRICKS_MCP_ENABLED=true` + config |
| `image-gen` | disabled by default | Needs `FAL_KEY` etc. |
| `video-gen` | disabled by default | Needs API keys |
| `tts` / `voice` | disabled | No audio devices |
| `kanban` | enabled, ephemeral | `kanban.db` stays in cache (not durable) |
| `messaging` (Telegram/Discord/Slack) | partial | Telegram works via outbound poller; others need their own poller |
| `databricks` (new) | enabled | Native Databricks tools (this repo) |

## Tool-level matrix

### Terminal / shell

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `terminal` | `tools/terminal_tool.py` (`TERMINAL_ENV=local`) | `in_app_subprocess` (default), `databricks_job`, `external_sandbox` | works-in-app | medium | cwd restricted to `<HERMES_HOME>/workspace`; default timeout 60s; output truncated at 64KB; user is the App SP. No interactive PTY. |
| `process_registry` | `tools/process_registry.py` | passthrough to backend | works-in-app | low | Process state file in `HERMES_HOME/processes.json`, syncs to UC Volume |

### File

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `read_file`, `write_file`, `edit_file`, `apply_patch` | `tools/file_tools.py`, `tools/file_operations.py` | `UCVolumeHome` (cache + sync) | works-with-uc-volume | low | All paths must resolve under `HERMES_HOME`; path guard rejects `..`, absolute escapes |
| `find_files`, `glob_files` | `tools/file_tools.py` | local cache walk | works-with-uc-volume | low | Only sees files synced into the cache; recommend `sync_from_volume()` before broad searches |
| `path_security` | `tools/path_security.py` | reused unchanged | works-in-app | low | Hermes' own path-traversal guard |

### Web / search

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `web_search` | `tools/web_tools.py` | outbound HTTPS | works-in-app | low | Default backend `tavily`/`exa`/`firecrawl` requires `requires-secret` |
| `web_fetch` | `tools/web_tools.py` | outbound HTTPS | works-in-app | low | `url_safety` allowlist applies; set `HERMES_ALLOW_PRIVATE_URLS=0` |
| `x_search` | `tools/x_search_tool.py` | outbound HTTPS | requires-secret | low | Needs Twitter/X bearer token |

### Browser

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `browser_*` | `tools/browser_tool.py`, `browser_cdp_tool.py`, `browser_supervisor.py` | `disabled` (default), `external_browser` (Browserbase), `databricks_job_browser` (future) | blocked-by-default | high | Returns a structured `BackendUnavailable` payload explaining how to enable a backend |
| `browser_camofox`, `browser_dialog` | `tools/browser_camofox.py`, `browser_dialog_tool.py` | same as above | blocked-by-default | high | Same diagnostic |

### Memory

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `memory_*` | `tools/memory_tool.py` | local cache (default) | works-with-uc-volume | low | `MEMORY.md` and per-session memory files; synced |
| `hindsight memory` plugin | `plugins/memory/hindsight/` | passthrough HTTPS | requires-secret | low | Needs `HINDSIGHT_API_KEY` |

### Skills

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `skill_list`, `skill_view`, `skill_create`, `skill_manage`, `skill_search`, `skills_hub`, `skills_sync` | `tools/skills_tool.py`, `tools/skill_manager_tool.py`, `tools/skills_sync.py`, `tools/skills_hub.py` | `UCVolumeHome` | works-with-uc-volume | low | `HERMES_OPTIONAL_SKILLS` can point at a UC Volume path to load read-only skills |

### Memory (session)

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `session_search` | `tools/session_search_tool.py` | Lakebase `search_messages` (ILIKE) | works-with-lakebase | low | Documented limitation vs. SQLite FTS5 |

### Cron

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `cron_create`, `cron_list`, `cron_update`, `cron_delete` | `tools/cronjob_tools.py` | `HERMES_HOME/cron/jobs.json` synced; tick scheduled by supervisor | works-with-uc-volume | low | Heavy jobs can be redirected to Databricks Jobs (future) |

### Code execution / delegation

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `code_execution` | `tools/code_execution_tool.py` | in-app sandboxed | works-in-app | medium | Runs through `terminal_backend` with `in_app_subprocess` |
| `delegate` | `tools/delegate_tool.py` | subprocess `AIAgent` in `asyncio.to_thread` | works-in-app | medium | Subagent inherits provider, session DB, and tool set |
| `mixture_of_agents` | `tools/mixture_of_agents_tool.py` | in-process | works-in-app | medium | LLM-bound, no host deps |

### MCP

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `mcp_*` | `tools/mcp_tool.py`, `mcp_oauth*.py` | `disabled` (default), `remote_http` | not-yet-validated | medium | Config in `HERMES_HOME/config.yaml`, tokens in `mcp-tokens/` (synced) |
| `computer_use` | `tools/computer_use_tool.py` | not-supported | blocked | high | macOS-only `cua-driver` |

### Messaging

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `send_message` (Telegram) | `tools/send_message_tool.py` | `telegram_polling.TelegramClient.send_message` | works-in-app | low | Outbound HTTPS only |
| `discord_*` | `tools/discord_tool.py` | not started | not-yet-validated | low | Same outbound pattern is possible |
| `feishu_*` | `tools/feishu_*` | not started | not-yet-validated | low | |

### Vision / media

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `vision_*` | `tools/vision_tools.py` | passthrough HTTPS | requires-extra | low | Some need extras |
| `image_generation` | `tools/image_generation_tool.py` | passthrough HTTPS | requires-secret | low | Needs `FAL_KEY` or alternative |
| `video_generation` | `tools/video_generation_tool.py` | passthrough HTTPS | requires-secret | low | |
| `tts`, `transcription` | `tools/tts_tool.py`, `tools/transcription_tools.py` | passthrough HTTPS | requires-extra | low | Local TTS extras not installed |

### Security / safety

| Tool | Hermes Source | Databricks Backend | Status | Risk | Notes |
|------|---------------|--------------------|--------|------|-------|
| `url_safety` | `tools/url_safety.py` | reused | works-in-app | low | Set `HERMES_ALLOW_PRIVATE_URLS=0` |
| `approval` | `tools/approval.py` | reused | works-in-app | low | |
| `tirith_security` | `tools/tirith_security.py` | downloads binary | not-yet-validated | medium | Binary lands in `HERMES_HOME/bin/tirith` (in cache, synced) |

### Databricks-native (new toolset)

| Tool | Source | Backend | Status | Risk | Notes |
|------|--------|---------|--------|------|-------|
| `databricks_serving_endpoint_status` | `hermes_databricks.tools.databricks_toolset` | `WorkspaceClient().serving_endpoints.get(...)` | works-in-app | low | Read-only |
| `databricks_volume_read` | same | `WorkspaceClient().files.download(...)` | works-in-app | low | Prefix-restricted |
| `databricks_volume_write_agent_note` | same | `WorkspaceClient().files.upload(...)` | works-in-app | low | Writes under `artifacts/agent-notes/` |
| `databricks_uc_describe_table` | same | `WorkspaceClient().tables.get(full_name)` | works-in-app | low | Allowlist enforced |
| `databricks_uc_query_readonly` | same | `WorkspaceClient().statement_execution.execute_statement(...)` | works-in-app | medium | SELECT/WITH-only; row limit; allowlist; warehouse id required |
| `databricks_jobs_list` | same | `WorkspaceClient().jobs.list()` | works-in-app | low | Filtered by SP visibility |
| `databricks_jobs_run_allowlist` | same | `WorkspaceClient().jobs.run_now(job_id)` | works-in-app | medium | Job id allowlist enforced |
| `databricks_terminal` | `hermes_databricks.tools.terminal_backend` | `in_app_subprocess` (default), `databricks_job`, `external_sandbox` | works-in-app | medium | Same guardrails as Hermes' `terminal`: cwd under `<HERMES_HOME>/workspace`, default 60s timeout, 600s hard cap, 64KB output cap, command denylist for `rm/dd/mkfs/shutdown/...` |

## Backend availability checks

The supervisor exposes `/debug/tools` with a JSON shape like:

```json
{
  "registered_tools": 287,
  "backends": {
    "terminal": {"selected": "in_app_subprocess", "available": true},
    "browser":  {"selected": "disabled",          "available": false,
                 "reason": "BROWSER_BACKEND=disabled"},
    "mcp":      {"selected": "disabled",          "available": false,
                 "reason": "HERMES_DATABRICKS_MCP_ENABLED=false"}
  },
  "databricks_toolset": ["databricks_serving_endpoint_status", "..."]
}
```

This is the canonical place to discover what's configured.
