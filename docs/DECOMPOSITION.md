# Decompose Bronson before adding GitLab

Status: extraction in progress. The executable now lives in `bronson/cli.py` and starts through
`python -m bronson`. Pure log presentation lives in `bronson/logs.py`, and log storage plus the
dashboard-v1 codec live in `bronson/cache.py`. Their owning tests are in `tests/`. The dashboard
receives a cache instance and still owns freshness and snapshot application. Process execution and
GitHub extraction are next. GitLab support has not shipped.

## Goal and boundary

Give each mechanism one owner so GitLab can be added without teaching every widget a second API.
Preserve the working GitHub UI, CLI, caches, and cancellation behavior during extraction. Keep
Python 3.10 and Textual. A second provider does not justify a language migration.

The first milestone is decomposition with GitHub only. GitLab comes next. Named workspace groups and
session restore follow that. Do not implement future modules, configuration formats, or commands
just to fill out this tree.

## Design choice

Two candidates were considered:

- Keep one file and add provider branches in the dashboard and log screens. This avoids import
  changes but spreads API fields, capabilities, credentials, and request identity across UI code.
- Extract a small package around API access, storage, log presentation, and UI ownership. This adds
  a few explicit dependencies but lets each rule change in one place and gives tests deterministic
  seams.

Choose the package. Split by responsibility, not line count. Do not add a plugin registry, event
bus, service container, generic repository layer, or controller per widget. Keep polling
orchestration in one dashboard until a second real consumer needs it.

## First-milestone layout

```text
bronson/
  __init__.py
  __main__.py           # calls cli.main, no startup on import
  cli.py               # argument parsing and dependency construction
  models.py            # repository identity, run/job records, capabilities
  providers/
    __init__.py        # narrow provider contract
    github.py          # remote resolution and GitHub API mapping/actions
    process.py         # cancellable, timed CLI execution, no Textual imports
  cache.py             # log files and dashboard snapshots
  logs.py              # log cleanup, severity classification, Rich rendering
  ui/
    __init__.py
    dashboard.py       # run list, polling, selection, mutation coordination
    jobs.py            # JobPane and JobDetailScreen
    log_viewer.py      # LogScreen, search, scrolling, failure navigation
    presentation.py    # table cells, time labels, status glyphs, FittedTable
    themes.py          # optional Flume integration and theme roles
    screens.py         # help and theme selection
scripts/
  format.sh
  ci-local.sh
tests/
  test_github.py
  test_process.py
  test_cache.py
  test_logs.py
  test_dashboard.py
```

The tree is an ownership map, not a requirement for one class per file. For example, keep the two
job views together because they share presentation and interaction rules. Add test modules as their
owners move, not as empty placeholders.

## Ownership and dependency rules

### CLI and repository identity

`cli.py` constructs the provider, cache, and dashboard. Preserve `--repo`, `--theme`, origin
detection, and existing exit behavior. The compatibility launcher must work from outside the
checkout as it does now. It may add the checkout root to the import path before importing
`bronson.cli`; production modules must not modify import paths.

Support `python -m bronson` from the checkout after extraction. Packaging and an installed `bronson`
command are separate work, not prerequisites for this refactor.

An immutable repository reference carries provider, hostname, and full project path. Do not
introduce an ambient mutable current repository. In the first milestone this reference represents
GitHub on `github.com`; do not claim enterprise support without implementing and testing it.

### Models and provider contract

`models.py` uses standard-library types and annotations at the boundary. Prefer small dataclasses
and explicit states over untyped response dictionaries. It imports neither Textual nor provider
modules.

Runs and jobs expose identity, display metadata, URLs, timestamps, native status, normalized phase
and outcome, and a revision token where needed. Retain the distinction between execution phase and
result. Unknown provider values remain visible rather than becoming success. Revision tokens are
opaque to widgets; GitHub owns how attempt, status, and update time affect freshness.

The provider owns:

- listing a bounded number of recent runs;
- loading a run and all its current jobs, including pagination;
- fetching a job log;
- retrying failed jobs, rerunning all jobs, and canceling a run;
- declaring supported actions and mapping provider errors into useful failures.

Methods receive operation names and IDs, not CLI argument arrays assembled by widgets. Capability
checks belong at the provider boundary as well as in UI affordances. Run state still determines
whether an action is currently applicable.

GitHub-specific fields such as `databaseId`, `headBranch`, and `conclusion` stop at the adapter.
Normalize once. During migration, convert one complete call path at a time and remove the old access
path rather than maintaining parallel model implementations.

### Process execution and cancellation

Move `run_process` out of the UI module. It currently discovers the Textual worker internally.
Replace that dependency with an explicit cancellation predicate supplied by the UI worker, with a
harmless default for synchronous callers and tests.

Preserve timeouts, child termination and reaping, stdout/stderr handling, and exception causes. Test
cancellation with a real short-lived subprocess. Do not silently change CLI authentication or output
limits in a mechanical extraction. Document existing limitations and add bounded-output behavior as
a separate tested change if needed.

The process runner does not select a host or discover credentials. Providers construct explicit
argv; no shell interpolation. Cancellation of a local process cannot establish whether a remote
mutation happened. Do not automatically replay mutations after timeout or cancellation.

### Cache and snapshots

Move `_cache_path`, `cache_get`, `cache_put`, snapshot validation, and atomic snapshot writes into
`cache.py`. The dashboard supplies data and applies a validated snapshot; storage does not reach
into widgets or call `apply_gates`.

Preserve existing cache keys and the dashboard-v1 format in this milestone. Existing snapshots
remain startup hints, not successful refreshes. Preserve size checks, revision matching, pruning,
temporary file cleanup, and graceful handling of unavailable storage. This refactor must not
introduce a second snapshot codec.

Keep in-memory request coordination and UI freshness decisions in the dashboard. Keep filesystem
policy in the cache owner. Cache paths are constructor inputs rather than module globals, so tests
use temporary directories without patching application internals.

Host/provider/workspace cache namespacing is required before multiple contexts ship. That later
change must version or deliberately discard legacy cache data rather than reinterpret it under
another host.

### Logs and presentation

`logs.py` may depend on Rich, but not Textual widgets, subprocesses, or cache paths. Keep existing
ANSI cleanup, severity detection, rendered sections, and diagnostic-versus-job-result distinctions
intact. The log viewer owns search state, scrolling, and highlights, not download commands.

`ui/presentation.py` owns shared time labels, cells, glyphs, and fitted tables. Do not invent a
generic `utils.py`. A shared helper belongs here only when it actually presents data.

### UI and theme integration

The dashboard remains the single owner of polling cadence, worker scheduling, stale-result checks,
selection, inline expansion, and mutation completion. Job and log screens consume application-owned
services rather than starting independent provider caches or polling loops.

Move CSS with the component it styles where practical. Keep theme loading and the optional Flume
bridge in `ui/themes.py`, without making Flume a required dependency. Preserve no-bridge fallback
and live repaint behavior.

Lower layers never import `ui`. Avoid circular imports by passing data and small callbacks to
screens, not importing the concrete dashboard class for runtime access. Do not build an event
framework to solve an import cycle.

## Extraction sequence

1. **Establish the baseline.** Apply the pinned format/lint rules, run the current suite, and record
   existing behavior. Keep this mechanical change separate from module moves.
2. **Extract pure presentation and storage.** Move log parsing/rendering, cache codecs, and their
   tests. Leave the dashboard orchestrating exactly the same behavior.
3. **Extract process execution and GitHub.** Introduce repository identity, normalized records, and
   the narrow provider contract. Replace UI-built CLI arguments. Keep public CLI behavior unchanged.
4. **Split the UI.** Move job and log screens, shared presentation, and theme integration. Keep one
   dashboard owner for polling and selection.
5. **Finish startup and test migration.** Add package entry points, use ordinary package imports
   in tests, and update `ci-local.sh` discovery in the same change.
6. **Review the boundary before GitLab.** Verify that widgets contain no GitHub response keys or
   command construction, storage has no widget dependencies, and process execution has no Textual
   dependency.

Every step ends with passing tests. Temporary compatibility forwarding is acceptable during a move,
but remove it before completing the milestone. Do not combine extraction with new keybindings, cache
formats, workspace persistence, or changed retry semantics.

## Proof and acceptance

Move tests to the interface that owns behavior. The current suite dynamically imports the executable
and patches globals. Replace those patches with injected fake providers, temporary cache roots, and
cancellation predicates. Keep UI tests for composition rather than duplicating all API mapping
cases.

Required coverage includes:

- GitHub pagination, latest-attempt jobs, normalization, exact mutation argv, and error handling;
- subprocess success, nonzero exit, timeout, cancellation, and reaping;
- log search inputs, ANSI cleanup, diagnostic highlighting, and failed-job filtering;
- cache hits, malformed snapshots, stale revisions, atomic writes, and failed-refresh fallback;
- inline expansion, narrow/wide layouts, resize selection, themes, preview reuse, and shutdown;
- compatibility launcher and module entry point behavior, including invocation outside the checkout.

Run the complete deterministic gate after each completed extraction slice. Manually smoke-test the
existing GitHub dashboard before handoff when authenticated access is available. Do not trigger real
reruns or cancellation merely to validate a refactor. Report missing live evidence explicitly.

Request an independent read-only review for changes to credential routing, cache
validation/recovery, or mutation lifecycle behavior. Formatting alone does not require that review.

The decomposition is done when existing behavior and tests pass, the CLI owns startup, all raw
GitHub API knowledge lives in the adapter, and each mechanism has one implementation owner. No
GitLab adapter or workspace implementation is necessary to satisfy this milestone.

## Follow-on work, not part of extraction

Add `providers/gitlab.py` next, using `glab` and explicit host routing. Its API mapping must account
for nested project paths, pagination, manual/scheduled jobs, allowed failures, job traces, and the
absence of pipeline-level rerun-all semantics. Re-read the current official APIs when implementing,
especially for the target self-managed GitLab version.

Then add `workspaces.py` for named repository groups and lightweight session restore. A workspace
may contain either provider. Credentials stay in `gh`/`glab`, not workspace files. Switching must
cancel local reads and reject late callbacks from an old context generation. Namespace local caches,
define environment-token precedence, and keep mutations attached to their original context before
enabling switching. Workspaces are not an OS security boundary.

Keep dashboard snapshots separate from session state: snapshots accelerate display; sessions
remember selection and filters. Neither is fresh provider truth. Freeze their configuration and
persistence formats only when that feature is implemented and tested.

API references for the next milestone: [Pipelines](https://docs.gitlab.com/api/pipelines/),
[Jobs](https://docs.gitlab.com/api/jobs/), [glab API](https://docs.gitlab.com/cli/api/), and
[glab authentication](https://docs.gitlab.com/cli/auth/login/).
