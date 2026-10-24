# Contributing to Bronson

Read [AGENTS.md](AGENTS.md) for engineering rules and
[the decomposition design](docs/DECOMPOSITION.md) for provider ownership and the remaining UI
extraction. GitLab access is read-only. Workspaces remain future work.

## Setup

Use Python 3.10 or newer with a virtual environment. Keep tool installation outside the checkout if
preferred:

```sh
python3 -m venv ~/.venvs/bronson
. ~/.venvs/bronson/bin/activate
python -m pip install -e . ruff==0.16.6 shfmt-py==4.2.0 shellcheck-py==0.11.0.1
npm install --global prettier@3.9.6 pyright@1.1.411
```

Node.js/npm install the Markdown formatter and type checker. They are not runtime dependencies.
Authenticate with `gh` or `glab` for live use, depending on the provider. Deterministic tests need
neither CLI nor authentication. Run `bronson` from any repository after activating this environment,
or use the editable pipx installation documented in [README.md](README.md).

To check the distributable separately from the editable checkout:

```sh
python -m pip wheel --no-deps --wheel-dir /tmp/bronson-wheels .
python -m venv /tmp/bronson-install
/tmp/bronson-install/bin/python -m pip install /tmp/bronson-wheels/bronson-*.whl
(cd /tmp && /tmp/bronson-install/bin/bronson --help)
```

Use fresh temporary directories for repeat runs. The installed command must work outside the
checkout without `PYTHONPATH`; package dependencies are declared in
[pyproject.toml](pyproject.toml).

## Style and formatting

Bronson adapts Kapsel's engineering style and pinned Python/Markdown tooling. It does not copy
Kapsel's Rust tooling or product-specific constraints. [ruff.toml](ruff.toml) is the Python settings
owner:

- Python 3.10-compatible syntax, four-space indentation, double quotes, and LF endings;
- a 100-column formatting target, allowing indivisible strings and URLs;
- lint rules `E4`, `E7`, `E9`, `F`, `I`, `B`, `UP`, and `C4` for basic errors, import ordering,
  likely bugs, Python 3.10 idioms, and unnecessary collection construction;
- Ruff 0.16.6, without preview rules.

Markdown and structured data use Prettier 3.9.6 with the repository's `.prettierrc.json`: 100
columns, wrapped prose, and two-space indentation. Shell scripts use shfmt 3.14.1 with two spaces;
ShellCheck 0.11.0 checks them without automatic fixes. TOML uses Taplo 0.10.0; install its
[prebuilt binary](https://github.com/tamasfe/taplo/releases) or a system package (for example,
`brew install taplo`). No Rust compiler is needed.

```sh
./scripts/format.sh             # format repository files
./scripts/format.sh check       # check only, no source rewrites
ruff check --no-cache --config ruff.toml .
```

The formatting script validates tool versions before writing, then sorts imports before formatting
Python. It does not apply other lint fixes. Inspect proposed fixes and apply only safe, understood
changes. Never enable unsafe fixes just to make the gate pass. Suppressions must be narrow and
explained.

Prefer precise names and coherent modules over comments or abstractions that only shorten a file.
Group validation, calculation, I/O, and cleanup visibly. Annotate maintained interfaces and
meaningful records with precise container elements. Use dataclasses for owned records and TypedDict
for display dictionaries. Keep execution phase separate from result and preserve unknown provider
states.

Annotations do not validate external data. Check consumed fields at the provider boundary, reject
malformed values explicitly, and keep diagnostics free of payloads and credentials. Catch specific
operating failures and preserve causes when adding context. Required validation must also work under
`python -O`. Keep cancellation and subprocess reaping intact during type or style changes.

Comments explain non-local invariants, compatibility, or why an obvious alternative is wrong. Caller
docs describe consequential obligations, not mandatory templates for every private helper.

## Type checking

Run the pinned Pyright 1.1.411 against the same environment used for tests:

```sh
./scripts/typecheck.sh
# Or select an environment without activating it:
PYTHON=~/.venvs/bronson/bin/python ./scripts/typecheck.sh
```

[pyrightconfig.json](pyrightconfig.json) explicitly includes `bronson` and `tests` in standard mode,
with Python 3.10 as the syntax target. Models, the GitHub adapter, and its tests use strict
checking. The script checks the tool version and never installs dependencies or rewrites files.

The remaining strict migration covers the GitLab adapter, repository/process helpers, cache, log and
theme owners, then dashboard state and their tests. Standard mode already checks these files, but
inferred unknown types and unannotated legacy interfaces remain. Narrow Textual app annotations name
the dashboard that owns these widgets without suppressing framework diagnostics. A single explained
test exception deliberately passes invalid input to exercise GitLab's runtime validation.

## Validation

The full local deterministic gate checks formatting, lint, types, unit and Textual integration
tests, and patch whitespace:

```sh
./scripts/ci-local.sh
```

It uses `python3` by default. To select another environment without activation:

```sh
PYTHON=~/.venvs/bronson/bin/python ./scripts/ci-local.sh
```

For a focused iteration, run the application tests or extracted log and cache tests directly:

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Provider mapping tests live in `tests/test_github.py` and `tests/test_gitlab.py`. Process and
repository identity tests exercise their extracted owners without Textual. Dashboard tests use fake
responses, including read-only GitLab navigation and cache isolation. No new test framework is
needed.

For documentation changes, also check that local file links and heading anchors resolve and that
planned behavior is not presented as shipped. The gate does not currently automate link checking.

For application changes, manually check affected interactions when possible. Keep live evidence
separate from deterministic tests, and do not rerun or cancel real workflows without authorization.
A passing mocked test does not prove authentication, terminal appearance, or compatibility with a
particular hosted instance.
