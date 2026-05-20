# Contributing

Thanks for taking the time to contribute. This document is the short
version; if anything is unclear, open an issue and we'll fix the
document.

## Code of conduct

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## How you can help

* **Bugs & regressions.** Open an issue using the
  [Bug report](.github/ISSUE_TEMPLATE/bug_report.yml) template. Include
  `databricks bundle validate -t free` output and the relevant
  `/debug/*` payload.
* **New tools.** Add a `_ToolSpec` to
  `app/trevor_databricks/tools/databricks_toolset.py` plus a unit test
  under `tests/unit/`. Tools should be allowlist-gated and never
  silently elevated.
* **New channels.** Mirror `app/trevor_databricks/telegram_polling.py`
  (outbound poll + allowlist + dispatch via `runtime.run_turn`).
* **Docs.** Anything that wasn't obvious when you onboarded.

Before you start something large, please open an issue first to make
sure it fits the project's scope.

---

## Development setup

```bash
git clone https://github.com/althrussell/trevor-ai.git
cd trevor-ai

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r app/requirements.txt
pip install -e ".[dev]"   # installs ruff + pytest from pyproject.toml
```

If you don't have a Databricks workspace handy, you can still run the
pure-Python checks:

```bash
PYTHONPATH=app python3 tests/run_offline_checks.py
```

These exercise the SQL guard, terminal sandbox, UC Volume path guard,
secret redaction, Databricks toolset arg validation, and the Telegram
allowlist using only the standard library.

For end-to-end work you'll need a Databricks workspace (Free Edition is
fine) and the Databricks CLI ≥ 0.245. See [QUICKSTART.md](QUICKSTART.md).

### Refreshing vendored ai-dev-kit skills

The 27 markdown skills under
[`app/seeds/skills/databricks/`](app/seeds/skills/databricks/) are
vendored from [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
at the tag pinned in
[`SKILLS_VERSION`](app/seeds/skills/databricks/SKILLS_VERSION). To pull
a newer upstream tag:

```bash
scripts/sync_databricks_skills.sh --tag v0.1.12  # or 'main' for the tip
git diff app/seeds/skills/databricks/             # review the churn
git add app/seeds/skills/databricks/
git commit -m "Refresh ai-dev-kit skills to v0.1.12"
```

The script regenerates `SKILLS_VERSION` and `INDEX.md`, preserves
`VENDOR_README.md`, and refreshes `UPSTREAM_LICENSE.md` /
`UPSTREAM_NOTICE.md` / `UPSTREAM_NOTICE.txt`. The bundled skills carry
the upstream Databricks License — see
[NOTICE.md §3](NOTICE.md#databricks-ai-dev-kit) for the scope-of-use
statement.

---

## Code style

| Concern | Tool / rule |
|---|---|
| Linting | `ruff check .` |
| Formatting | `ruff format .` (Black-compatible) |
| Type hints | Encouraged on new code; not enforced |
| Docstrings | Required on public functions in `trevor_databricks/` |
| Comments | Explain *why*, not *what*. The CI grumbles about narrative comments |
| Logging | Use the module-level `log = logging.getLogger(__name__)` — never `print` from runtime code |
| Secrets | Never log a raw token; the redacting formatter is a safety net, not an excuse |

CI runs `ruff check .` and `ruff format --check .`. Both must pass.

---

## Tests

| Test | When to add it |
|---|---|
| `tests/unit/test_<area>.py` (pytest) | New function, new tool, new guard |
| `tests/run_offline_checks.py` | Anything that should run with stdlib only — guards, validators, allowlists |
| `tests/integration/` (manual) | Anything that needs a real workspace |
| `scripts/smoke_test.sh` extension | New `/debug/*` endpoint |

Run locally:

```bash
PYTHONPATH=app python3 tests/run_offline_checks.py
PYTHONPATH=app pytest tests/unit -v
```

PRs that change behaviour without a test will be asked to add one.

---

## Pull request flow

1. **Branch off `main`.** Name it `feat/...`, `fix/...`, or `docs/...`.
2. **Make the change small and focused.** One concern per PR.
3. **Run locally:**
   ```bash
   ruff format .
   ruff check .
   PYTHONPATH=app python3 tests/run_offline_checks.py
   PYTHONPATH=app pytest tests/unit -v
   databricks bundle validate -t free   # if you touched anything under resources/, app/app.yaml, or databricks.yml
   ```
4. **Write the PR description** using the template — what / why /
   tested / risk.
5. **CI** must be green. The
   [`ci.yml`](.github/workflows/ci.yml) workflow runs ruff, the
   offline checks, the pytest suite, and a `bundle validate` against
   a stubbed Databricks profile.
6. **One approving review** is required to merge.

We prefer **squash merge** with a clean commit message — see below.

---

## Commit conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<optional body explaining why>

<optional footer: BREAKING CHANGE: ..., Closes #123>
```

Types we use:

| Type | When |
|---|---|
| `feat` | New user-visible behaviour |
| `fix` | Bug fix |
| `docs` | README, QUICKSTART, docstrings only |
| `refactor` | Internal restructuring with no behaviour change |
| `perf` | Perf improvement |
| `test` | Adding or fixing tests only |
| `build` | `pyproject.toml`, `requirements.txt`, bundle plumbing |
| `ci` | `.github/workflows` only |
| `chore` | Everything else |

Examples:

```
feat(toolset): add databricks_vector_search_query with read-only guard
fix(databricks_provider): strip exclusiveMaximum from integer schemas
docs: add Telegram allowlist guidance to QUICKSTART
ci: cache pip wheels across CI runs
```

---

## Release process

Releases are tagged from `main` and follow [semver](https://semver.org/):

* `0.x` — public POC; breaking changes can land in any minor.
* Bump in [`CHANGELOG.md`](CHANGELOG.md) using
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
* Tag: `git tag -s v0.x.y -m "..."` and push.
* GitHub release is created from the tag (CI builds and uploads
  `databricks bundle validate` artefacts).

---

## Licensing & attribution

This project is MIT-licensed. By contributing you agree your
contribution will be licensed under the same MIT terms (the standard
inbound = outbound model — no separate CLA).

### What you can pull in

| Source | OK to vendor? | OK to take patterns from? |
|---|---|---|
| MIT / Apache-2.0 / BSD-2/3-Clause / ISC libraries | yes (with header + NOTICE update) | yes |
| LGPL libraries (e.g. `psycopg`) | yes as a *runtime dependency* (dynamic linking); **no** as vendored source unless you ship LGPL terms | yes |
| GPL-licensed code | **no** — incompatible with MIT | yes (ideas only) |
| Code under a custom restrictive license (e.g. the Databricks "DB License" used by [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)) | **no** | only if the patterns are non-copyrightable (APIs, file layouts, governance shapes) |
| Code with no LICENSE file (e.g. Living-AI at the time of writing) | **no** — default copyright = all rights reserved | yes (patterns / architecture only — never expressive code) |

If you add a new runtime dependency, update
[`NOTICE.md`](NOTICE.md) in the same PR.

### SPDX header convention (new files only — don't churn existing ones)

New Python files should start with a single SPDX identifier comment so
license-scanning tools can pick up the licensing without parsing
LICENSE:

```python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Al Thrussell
"""Module docstring goes here."""
```

For YAML / shell / Markdown:

```yaml
# SPDX-License-Identifier: MIT
```

Files that vendor code from a permissively-licensed upstream must
include both license identifiers and a `Source:` line, e.g.:

```python
# SPDX-License-Identifier: MIT AND Apache-2.0
# Source: https://github.com/example/upstream commit <sha>
```

When in doubt, ask in your PR description and we'll figure it out.

## Architectural ground rules

These are non-negotiable; PRs that violate them will be asked to
restructure.

1. **Never vendor or modify `hermes-agent` core.** Compatibility goes
   in `app/trevor_databricks/`. If Hermes needs a hook, file an
   upstream issue first.
2. **Tools are never silently disabled.** If a backend is missing,
   the tool stays registered and returns a `BackendUnavailable`
   diagnostic.
3. **No direct local disk I/O outside `UCVolumeHome`.** Everything
   under `HERMES_HOME` belongs on UC.
4. **No secrets in environment variables.** Use Databricks Secrets +
   resource bindings.
5. **No anonymous endpoints.** Apps are workspace-OAuth gated, plus
   we still allowlist `/debug/*` actions.
6. **Every long-running async task gets a `health_probe` registered
   with the supervisor.** Silent crashes are forbidden.
7. **Lakebase DDL stays idempotent.** It runs on every boot through
   `LakebaseSessionDB.ensure_schema` and once via the setup job; both
   paths must tolerate "already exists" and (for the App SP path)
   "permission denied" cleanly.

---

## Questions?

Open an issue with the `question` label, or DM in the discussion
forum once we set one up.

Thanks again.
