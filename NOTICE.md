# NOTICE

Hermes on Databricks — third-party attributions and notices.

This project is distributed under the [MIT License](LICENSE). The
attributions below cover (a) upstream open-source projects this
project depends on at runtime, and (b) projects that served as
architectural references during development.

Nothing in this NOTICE supersedes the terms of the projects' own
licenses. If you redistribute this project (modified or not), you
must retain this file alongside [LICENSE](LICENSE).

---

## 1. Runtime dependencies (declared in `app/requirements.txt`)

The Databricks App installs the following packages at boot. Versions
reflect the pins (or floors) in `app/requirements.txt` and the deep
dependency tree of `hermes-agent==0.14.0`. Licenses are reported as
declared by each project's package metadata at the time of writing
(2026-05-19).

| Package | Pin | License | Upstream |
|---|---|---|---|
| `hermes-agent` | `==0.14.0` | MIT | <https://github.com/NousResearch/hermes-agent> |
| `databricks-sdk` | `==0.105.0` | Apache-2.0 | <https://github.com/databricks/databricks-sdk-py> |
| `fastapi` | `==0.115.5` | MIT | <https://github.com/fastapi/fastapi> |
| `uvicorn[standard]` | `==0.32.1` | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| `psycopg[binary]` | `==3.2.3` | LGPL-3.0 (see note below) | <https://github.com/psycopg/psycopg> |

### Hermes-agent (transitive — managed by Hermes' own pins)

These are pinned by `hermes-agent==0.14.0`'s own `setup.py` / `pyproject.toml`
and are not directly pinned by us. Licenses listed are the upstream
declarations.

| Package | License | Upstream |
|---|---|---|
| `openai` | Apache-2.0 | <https://github.com/openai/openai-python> |
| `pydantic` | MIT | <https://github.com/pydantic/pydantic> |
| `pydantic-core` | MIT | <https://github.com/pydantic/pydantic-core> |
| `httpx` | BSD-3-Clause | <https://github.com/encode/httpx> |
| `httpcore` | BSD-3-Clause | <https://github.com/encode/httpcore> |
| `requests` | Apache-2.0 | <https://github.com/psf/requests> |
| `PyYAML` | MIT | <https://github.com/yaml/pyyaml> |
| `jinja2` | BSD-3-Clause | <https://github.com/pallets/jinja> |
| `croniter` | MIT | <https://github.com/kiorky/croniter> |
| `python-dateutil` | Apache-2.0 / BSD-3-Clause (dual) | <https://github.com/dateutil/dateutil> |

This list is not exhaustive — run `pip show <package>` inside a
deployed App container, or `pip licenses` after a local install, for
the full resolved tree.

### Note on `psycopg[binary]` and LGPL-3.0

`psycopg` v3 is licensed under the GNU Lesser General Public License
v3.0 (LGPL-3.0). LGPL allows non-LGPL projects (including MIT-licensed
ones like this project) to depend on it via dynamic linking, which is
how Python packages work. End users may freely substitute their own
build of `psycopg` by changing `app/requirements.txt`.

We do not modify `psycopg`. If you fork this project and modify
`psycopg` itself, you must distribute your modifications under LGPL-3.0
per its terms.

The LGPL-3.0 text is available at
<https://www.gnu.org/licenses/lgpl-3.0.html>.

---

## 2. Dev-time dependencies (declared in `pyproject.toml [project.optional-dependencies] dev`)

These are not shipped in the App container; they run only on
contributor machines and in CI.

| Package | License | Upstream |
|---|---|---|
| `ruff` | MIT | <https://github.com/astral-sh/ruff> |
| `pytest` | MIT | <https://github.com/pytest-dev/pytest> |
| `pytest-asyncio` | Apache-2.0 | <https://github.com/pytest-dev/pytest-asyncio> |

---

## 3. Upstream architectural references

These projects were consulted during the design and porting work that
produced this repository. They do not contribute code; they contribute
ideas. Patterns and APIs are not copyrightable, but the projects are
credited here so contributors can read them in context.

### NousResearch Hermes Agent

* Repository: <https://github.com/NousResearch/hermes-agent>
* License: MIT (Copyright (c) 2025 Nous Research)
* Used here as: a runtime dependency installed via pip
  (`hermes-agent==0.14.0`). The Hermes `AIAgent`, tool registry,
  `run_conversation` loop, cron scheduler, and `SessionDB` interface
  are imported unmodified. This project **does not vendor or
  modify** Hermes core; all integration code lives in
  `app/hermes_databricks/`.

### Living-AI

* Repository: <https://github.com/vbalasu/living-ai>
* License: no LICENSE file declared at the time of writing
  (2026-05-19), so default copyright law applies — all rights
  reserved to the upstream author(s).
* Used here as: an **architectural pattern reference only** for
  the "agent-on-Databricks-Free-Edition" substrate — specifically,
  the high-level shape of running a long-lived FastAPI agent in a
  Databricks App with Lakebase Postgres state, Unity Catalog
  Volume storage, and outbound Telegram polling.
* Scope of use: ideas (architecture, lifecycle, sequencing, choice
  of services) only. **No source code from Living-AI is vendored,
  copied, or adapted in this project.** A line-level overlap
  analysis (see project history) confirmed that filename collisions
  and shared "substantive" lines are limited to Databricks SDK /
  Telegram Bot API / psycopg call signatures, mandatory Databricks
  notebook idioms (`dbutils.widgets.*`, `spark.sql(...)`), and
  engineering-judgment constants — all of which are either
  non-copyrightable expression (merger doctrine), standard stock
  elements (scènes à faire), or short factual selections.
* If you are the Living-AI author and would prefer different
  credit wording — or no credit at all — please open an issue at
  <https://github.com/althrussell/trevor-ai/issues> and we will
  amend.

### Databricks AI Dev Kit

* Repository: <https://github.com/databricks-solutions/ai-dev-kit>
* License: "Databricks License" — a custom license restricting use
  to "in connection with your use of the Databricks Services".
* Used here as: **not used**. This project does not depend on,
  import, vendor, or adapt code from `ai-dev-kit`. The license-
  compliance review for this NOTICE explicitly confirmed zero
  references in the codebase (`rg -i 'ai-dev-kit|ai_dev_kit|
  databricks-solutions|databricks_tools_core|fastmcp|
  claude-agent-sdk'` returns no matches).
* The `ai-dev-kit` README's structure (LICENSE / NOTICE / SECURITY
  / CONTRIBUTING / CODEOWNERS / dependency table) was reviewed as
  a *governance* pattern — ideas about how to organise a
  Databricks-adjacent OSS repo are not copyrightable, and the
  governance files in this repo are independently authored.

### Anthropic Claude (development tooling)

* Used here as: AI-assisted development tooling (the author wrote
  this codebase with Claude as a pair-programming assistant).
  Claude does not contribute copyrighted text from training data
  to the codebase; all code is reviewed and accepted by a human
  author.
* No Anthropic SDK, model, or product is a runtime dependency of
  this project.

---

## 4. Acknowledgements

* The Lakebase Postgres + Unity Catalog Volumes + Databricks Apps
  pattern is a Databricks Foundation Model serving + Field
  Engineering collective effort. The product names are trademarks
  of Databricks, Inc.
* The Hermes agent runtime is built and maintained by
  [Nous Research](https://nousresearch.com/). Without it, this
  repository would be vastly larger and substantially less
  capable.

---

## 5. Reporting an attribution issue

If you believe this NOTICE misses a required attribution, lists an
incorrect license, or misrepresents the use of an upstream project,
please open an issue at
<https://github.com/althrussell/trevor-ai/issues> using the
"feature request" template and label it `license-compliance`. We
treat license-compliance issues with the same urgency as security
reports — see [SECURITY.md](SECURITY.md).
