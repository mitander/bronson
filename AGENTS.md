# Bronson contributor guide

Bronson is a Python/Textual terminal UI for GitHub Actions. GitLab and workspaces are planned, not
implemented. This repository owns its code, technical design, and tests.

## Start here

1. Check Git status and preserve unrelated work. Do not commit or push unless asked.
2. Read [README.md](README.md) for current behavior and
   [the decomposition design](docs/DECOMPOSITION.md) before structural changes.
3. Inspect the direct implementation and tests. The application entrypoint is `bronson/cli.py`,
   with module startup in `bronson/__main__.py`. Dashboard tests live in
   `tests/test_dashboard.py`. Log presentation and storage live in `bronson/logs.py` and
   `bronson/cache.py`, tested in `tests/`. The rest of the design document's package tree remains
   a target.
4. Follow [CONTRIBUTING.md](CONTRIBUTING.md) for setup, formatting, lint, and validation.

## Engineering

Adapt Kapsel's simplicity criterion to this application: minimize the knowledge needed to understand
and safely change a rule, not the number of lines. Prefer deep modules with small, complete
interfaces. Split unrelated responsibilities, not files that merely exceed a line-count preference.

- Give each mechanism one implementation owner. Avoid change amplification, hidden dependencies, and
  parallel truth sources. Compare concrete alternatives before adding a durable format or boundary.
- Keep Python 3.10 compatibility. Prefer the standard library and existing dependencies. Do not add
  frameworks, plugin registries, generic utilities, or speculative provider machinery.
- Keep API mapping and command construction out of widgets. Pass repository identity and
  cancellation explicitly. Keep pure log presentation separate from downloads and storage.
- Use annotations at meaningful boundaries. Distinguish phase, result, cached data, and uncertainty.
  Unknown provider states must not become success.
- Handle input and operating failures explicitly. Preserve exception causes. Assertions are for
  programmer-controlled invariants, not network responses or cache contents.
- Keep timeouts and cleanup of owned subprocesses. Bound I/O and durable growth where the mechanism
  requires it. A local timeout or cancellation does not prove a remote action failed. Never blindly
  replay a mutation.
- Treat logs, API responses, and cached data as untrusted. Do not let them supply credentials or
  command authority. Never store credentials in snapshots or workspace configuration, or expose them
  in errors.
- Keep extraction behavior-preserving. Follow the staged design before adding GitLab or workspaces.

## Validate and document

Run focused tests while iterating, then `./scripts/ci-local.sh` before handoff. Formatting and lint
are separate checks. Do not use unsafe automatic lint fixes or broad suppressions.

Tests belong at the owning interface. Use fake providers and temporary storage for deterministic
checks. Never trigger real workflow mutations without explicit authorization. Independent read-only
review is required for credential routing, cache recovery/validation, or mutation lifecycle changes.
Report missing review or live verification rather than implying it passed.

Write short, concrete documentation for a technical newcomer. Explain why before listing rules. Link
to the canonical owner instead of duplicating contracts. Keep future plans distinct from shipped
behavior. Documentation changes still check local links and anchors, formatting, and
`git diff --check`.

Use a domain-oriented imperative commit subject when a commit is requested, for example
`providers: isolate GitHub API mapping`. At handoff, say what changed, what was checked, and what
remains unproved. No mandatory report template is needed.
