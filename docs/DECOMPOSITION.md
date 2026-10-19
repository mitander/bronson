# Decompose Bronson before adding GitLab

Status: provider extraction is implemented. Startup and widgets still live in `bronson/cli.py`. Pure
log presentation lives in `bronson/logs.py`, and storage plus the dashboard-v1 codec live in
`bronson/cache.py`. `bronson/models.py` owns repository identity and typed display records.
`bronson/providers/` owns process execution, repository resolution, GitHub access, and read-only
GitLab access. Tests live in `tests/`. Splitting the UI into the target tree below remains future
work, not a prerequisite for the shared provider interface.

## Goal and boundary

Give each mechanism one owner so GitLab can be added without teaching every widget a second API.
Preserve the working GitHub UI, CLI, caches, and cancellation behavior during extraction. Keep
Python 3.10 and Textual. A second provider does not justify a language migration.

Provider extraction preserved GitHub behavior before adding read-only GitLab. The remaining UI file
moves are independent of provider access, so they are deferred rather than bundled into the second
backend. Named workspace groups and session restore remain future work. Do not implement modules,
configuration formats, or commands just to fill out this tree.

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
detection, and existing exit behavior. `scripts/bronson.py` is retired; use the installed `bronson`
command or `python -m bronson`. Both preserve the caller's working directory for origin detection.
Production modules must not modify import paths. Terminal launchers own failed-window retention, not
Bronson.

[Package metadata](../pyproject.toml) owns dependencies and the installed console entry point.
Editable installations support a live checkout without external wrappers or `PYTHONPATH` wiring.

An immutable repository reference carries provider, hostname, and full project path. There is no
ambient mutable current repository. GitHub access supports `github.com`. GitLab access uses an
explicit host on every request. Unknown origin hosts require `--provider gitlab`, rather than being
guessed from their names. Enterprise GitHub support remains unimplemented.

### Models and provider contract

`models.py` uses standard-library types and annotations at the boundary. Repository identity and
run-detail enrichment use dataclasses. Runs and jobs use typed display dictionaries, retaining the
existing dashboard-v1 field vocabulary to avoid a second codec or a cache migration during
extraction. These are shared display records, not arbitrary API responses. The model owner imports
neither Textual nor provider modules.

Runs and jobs expose identity, display metadata, URLs, timestamps, native status, normalized phase
and outcome, and a revision token where needed. Retain the distinction between execution phase and
result. Unknown provider values remain visible rather than becoming success. Revision tokens are
opaque to widgets; GitHub owns how attempt, status, and update time affect freshness.

The provider interface owns:

- listing a bounded number of recent runs;
- loading current jobs, including pagination, and optional title/timing metadata;
- fetching a job log;
- retrying failed jobs, rerunning all jobs, and canceling a run when supported;
- declaring supported actions and mapping provider errors into useful failures.

Methods receive operation names and IDs, not CLI argument arrays assembled by widgets. Capability
checks belong at the provider boundary as well as in UI affordances. Run state still determines
whether an action is currently applicable.

Normalize API responses once in each adapter. Widgets consume only the shared display fields, never
native response payloads or CLI argument arrays. The retained display vocabulary includes
`databaseId`, `headBranch`, and `conclusion`. GitLab maps pipeline IDs, refs, and results into those
same fields. Execution phase and result remain distinct, and unknown status strings remain visible.
Remove old access paths rather than maintaining parallel implementations.

### Process execution and cancellation

`providers/process.py` owns `run_process`. It receives an explicit cancellation predicate supplied
by the UI worker, with a harmless default for synchronous callers and tests. It has no Textual
dependency.

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

GitLab cache identities include provider, host, and full project path. GitHub on `github.com`
retains its legacy identity. Legacy data is never reinterpreted under a different host or provider.
Both snapshot payloads and file keys include the repository identity. Workspace namespacing remains
future work.

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

Move CSS with the component it styles where practical. Theme loading currently lives in
`bronson/themes.py`; its eventual UI owner is `ui/themes.py`. It reads optional Flume palette files
directly, without an external Python bridge or a required Flume installation. Preserve built-in
fallback and live repaint behavior.

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
5. **Finish startup and test migration.** Add package entry points, use ordinary package imports in
   tests, and update `ci-local.sh` discovery in the same change.
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

## Read-only GitLab boundary

`providers/gitlab.py` uses `glab api --hostname HOST --method GET`. It URL-encodes the full project
path as one API segment. It fetches at most 100 recent top-level pipelines and paginates current
jobs, excluding retried attempts. A pagination safety limit fails the request instead of presenting
partial jobs as complete. Pipeline details supply timing, and job commit metadata supplies titles.
Child pipelines and bridge jobs are not expanded.

Manual and scheduled states stay unfinished. Allowed failures retain failure results and carry an
explicit label. Unknown states never become success. Completed job traces use the same presentation
and cache as GitHub. Running traces are not fetched or cached.

GitLab declares no mutation capability, rejects mutation calls at the provider boundary, and the UI
blocks `r`, `R`, and `c`. Saved `glab` authentication owns host-specific credentials. Ambient token
and CI auto-login overrides are removed from the child environment. Ambient API-host and protocol
overrides are removed too. Account and routing authority stays in the saved host settings, so an
origin cannot redirect an unrelated environment token to another host. No authentication credentials
enter display records or snapshots. Read-only fake-provider tests do not establish live self-managed
version compatibility.

## Follow-on work

Finish the independent UI file moves when needed. Add `workspaces.py` for named repository groups
and lightweight session restore. A workspace may contain either provider. Credentials stay in
`gh`/`glab`, not workspace files. Switching must cancel local reads and reject late callbacks from
an old context generation. Namespace local caches, define environment-token precedence, and keep
mutations attached to their original context before enabling switching. Workspaces are not an OS
security boundary.

Keep dashboard snapshots separate from session state: snapshots accelerate display; sessions
remember selection and filters. Neither is fresh provider truth. Freeze their configuration and
persistence formats only when that feature is implemented and tested.

GitLab API references: [Pipelines](https://docs.gitlab.com/api/pipelines/),
[Jobs](https://docs.gitlab.com/api/jobs/), [glab API](https://docs.gitlab.com/cli/api/), and
[glab authentication](https://docs.gitlab.com/cli/auth/login/).
