# Vendored databricks-skills/ from ai-dev-kit

This directory is a verbatim copy of the `databricks-skills/` subtree of
[databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
at the upstream tag recorded in [SKILLS_VERSION](SKILLS_VERSION).

It is bundled into the Trevor app and seeded into `HERMES_HOME/skills/databricks/`
on first boot, where Hermes' `skill_list`/`skill_view`/`skill_search` tools
surface it to the agent at runtime.

## License

The vendored content is distributed under the upstream **Databricks
License** ([UPSTREAM_LICENSE.md](UPSTREAM_LICENSE.md)), which permits
redistribution provided that use is "within or connecting to the Databricks
Services". Trevor runs as a Databricks App, so this use case qualifies.

Per the upstream license:

- A copy of the license is co-located here.
- The upstream NOTICE files ([UPSTREAM_NOTICE.md](UPSTREAM_NOTICE.md) and
  [UPSTREAM_NOTICE.txt](UPSTREAM_NOTICE.txt)) ride along unchanged.
- Modifications to upstream files must carry "prominent notices stating
  that you changed the files". **If you modify any skill below, add a
  `Modified-by-trevor:` line at the top of that SKILL.md.**

The Trevor project itself remains MIT-licensed (see the repo root
`LICENSE`). The Databricks License applies only to the contents of this
`databricks/` directory.

## Refreshing this vendor copy

Use [scripts/sync_databricks_skills.sh](../../../../scripts/sync_databricks_skills.sh)
to refresh against a different upstream tag. The script overwrites
everything in this directory except `VENDOR_README.md` and `SKILLS_VERSION`
(which it regenerates with the new pin).
