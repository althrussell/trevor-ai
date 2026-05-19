<!--
Thanks for opening a PR. Please fill out the sections below.
Delete the ones that don't apply. Small, focused PRs land faster.
-->

## What

<!-- One or two sentences. What does this change? -->

## Why

<!--
Link an issue if there is one (Closes #123). Otherwise: what user
problem does this solve, and why this approach over the alternatives?
-->

## How

<!-- Implementation notes: what files / modules / contracts changed. -->

## Tested

<!--
Tick all that apply. CI will run the first three automatically — only
include manual verification details below.
-->

- [ ] `ruff format --check .`
- [ ] `ruff check .`
- [ ] `PYTHONPATH=app python3 tests/run_offline_checks.py`
- [ ] `PYTHONPATH=app pytest tests/unit -v`
- [ ] `databricks bundle validate -t free`
- [ ] Deployed to a Free Edition workspace and exercised `/debug/*`
- [ ] Telegram round-trip verified

<!-- Paste the relevant `/debug/*` JSON or terminal output if helpful. -->

## Risk / blast radius

<!--
What could break? What's the rollback story? Anything in here that's
behind a feature flag or env var?
-->

## Checklist

- [ ] My change preserves the **"never vendor or modify `hermes-agent`
      core"** invariant.
- [ ] My change preserves the **"tools are never silently disabled"**
      invariant.
- [ ] I added or updated unit tests where the change is testable in
      pure Python.
- [ ] I updated `README.md` / `QUICKSTART.md` / `docs/*` where the
      change is user-visible.
- [ ] I added a bullet to `CHANGELOG.md` under `[Unreleased]`.
- [ ] My commit messages follow
      [Conventional Commits](https://www.conventionalcommits.org/)
      (e.g. `feat(toolset): …`, `fix(databricks_provider): …`).
- [ ] No secrets, PATs, or bearer tokens in the diff or tests.
