# Contributing

Bronson is a Python/Textual application. Start with the [README](README.md) to install and use it.
This guide covers contributor setup, checks and the main code boundaries.

## Setup

Use Python 3.10 or newer. From the repository root, create and activate a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . ruff==0.16.6 shfmt-py==4.2.0 shellcheck-py==0.11.0.1
npm install --global prettier@3.9.6 pyright@1.1.411
```

Node.js/npm run contributor tools, not Bronson. TOML formatting also needs
[Taplo 0.10.0](https://github.com/tamasfe/taplo/releases), available as a prebuilt binary or system
package. The Python shfmt package supplies shfmt 3.14.1. No Rust compiler is needed.

Authenticate with `gh` or `glab` for live use. Deterministic tests need neither CLI nor credentials.
You can keep the virtual environment outside the checkout instead, but use the same interpreter for
types and tests.

## Check a change

```sh
./scripts/format.sh       # format Markdown, Python, data and shell files
./scripts/ci-local.sh     # formatting, lint, types, tests and patch whitespace
```

[GitHub Actions](.github/workflows/ci.yml) runs the same gate on Ubuntu with Python 3.10 and 3.14
for pushes and pull requests. It also builds a wheel and checks both entry points from a fresh
installation outside the checkout. CI needs no provider credentials and makes no live workflow
requests.

Checks do not install missing tools. The gate uses `python3` by default. Set
`PYTHON=/path/to/venv/bin/python` to select another environment without activating it.

For focused checks:

```sh
./scripts/format.sh check
ruff check --no-cache --config ruff.toml .
./scripts/typecheck.sh
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s tests -p 'test_gitlab.py'
```

[ruff.toml](ruff.toml) owns Python formatting and lint rules. Keep Python 3.10 syntax, four-space
indentation, double quotes and a 100-column target. Markdown and structured data use
[Prettier](.prettierrc.json) with wrapped prose at 100 columns. Shell scripts use shfmt and
ShellCheck. The formatter sorts Python imports but does not apply other lint fixes. Review lint
repairs separately and do not use unsafe fixes or broad suppressions.

[pyrightconfig.json](pyrightconfig.json) checks application code and tests in standard mode, with
strict checking for models, the GitHub adapter and its tests. The remaining modules are checked, but
still have unannotated interfaces and inferred unknown types.

Review local Markdown links and anchors after documentation changes. The gate does not automate link
checking. Mocked tests do not prove live authentication, terminal appearance or compatibility with a
particular GitLab instance. Keep live checks read-only unless workflow mutations are explicitly
authorized.

## How the code fits together

| Owner                | Responsibility                                                                           | Tests                                                                       |
| -------------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `bronson/cli.py`     | Startup, Textual views, polling, selection and action coordination                       | `test_cli.py`, `test_dashboard.py`, `test_gitlab_dashboard.py`              |
| `bronson/models.py`  | Repository identity, run/job display records and revision identity                       | Provider and dashboard tests                                                |
| `bronson/providers/` | Provider operations, GitHub/GitLab mapping, repository resolution and timed subprocesses | `test_github.py`, `test_gitlab.py`, `test_repository.py`, `test_process.py` |
| `bronson/cache.py`   | Raw log storage and dashboard-v1 startup snapshots                                       | `test_cache.py`                                                             |
| `bronson/logs.py`    | Log cleanup, severity detection and Rich rendering                                       | `test_logs.py`                                                              |
| `bronson/themes.py`  | Optional Flume palettes and active-theme watching                                        | `test_themes.py`                                                            |

Test files live in `tests/`. `python -m bronson` and the installed `bronson` command both call
`cli.main` without changing the caller's directory. The dashboard receives a provider and cache.
Providers construct CLI arguments and normalize responses once, so widgets do not need to know
native API payloads. The shared display fields retain GitHub-style names for snapshot compatibility.

The dashboard owns refresh and in-flight requests. Run revisions distinguish attempts, status and
update time, so late job details cannot replace a newer revision. Unchanged completed details are
reused. Snapshots are validated startup hints, not successful refreshes. Storage failure is a cache
miss rather than an empty provider response. GitLab cache identities include provider, hostname and
full project path, while GitHub retains its existing cache identity.

GitLab uses explicit host-scoped `glab api --method GET` requests. It declares no mutation
capability and rejects mutation calls as well as hiding them in the UI. Saved `glab` settings own
host credentials and routing. Ambient token, CI auto-login and API-routing overrides are removed
from its subprocess environment. GitHub uses `gh` with its normal authentication environment.

The process runner uses argument lists, timeouts and explicit cancellation. It kills and reaps its
owned child when a request is cancelled or times out. Captured output is buffered in memory, not
size-bounded. Local cancellation does not establish whether GitHub accepted a mutation. Do not retry
a mutation automatically after an uncertain result.

## Keep changes small

Split responsibilities when doing so hides a rule, not just to shorten a file. Reuse the standard
library and existing dependencies. Keep API mapping and command construction out of widgets, and log
presentation separate from downloads and storage. Workspaces and session restore are not
implemented. Do not add speculative configuration or provider machinery for them.

Annotate maintained interfaces and meaningful records. Validate external fields before using them,
keep unknown provider states visible, and distinguish execution phase from result. Assertions are
for programmer-controlled invariants, not response validation or required side effects. Keep errors
free of credentials and raw payloads. Comments should explain an invariant, ownership or a tradeoff,
not narrate obvious code.

Put tests at the owning interface, using fake providers and temporary storage. Preserve unrelated
edits and user data. Changes to credential routing, cache validation/recovery, hostile-input
handling or mutation lifecycle need independent read-only review as well as tests. Missing required
evidence means the change is incomplete. Do not commit, publish or deploy without explicit
permission.

## Check the installed package

Use fresh temporary directories to test the wheel separately from the editable checkout:

```sh
wheel_dir=$(mktemp -d)
install_dir=$(mktemp -d)
python -m pip wheel --no-deps --wheel-dir "$wheel_dir" .
python -m venv "$install_dir"
"$install_dir/bin/python" -m pip install "$wheel_dir"/bronson-*.whl
(cd "$install_dir" && bin/bronson --help && bin/python -m bronson --help)
```

Both commands should show help outside the checkout without `PYTHONPATH`. Runtime dependencies and
the console entry point belong in [pyproject.toml](pyproject.toml).
