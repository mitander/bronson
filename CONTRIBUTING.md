# Contributing to Bronson

Read [AGENTS.md](AGENTS.md) for engineering rules and
[the decomposition design](docs/DECOMPOSITION.md) for the planned package boundaries. The
decomposition is the first milestone; GitLab and workspaces come afterward.

## Setup

Use Python 3.10 or newer with a virtual environment. Keep tool installation outside the checkout if
preferred:

```sh
python3 -m venv ~/.venvs/bronson
. ~/.venvs/bronson/bin/activate
python -m pip install textual ruff==0.16.6
npm install --global prettier@3.6.2
```

Node.js/npm are only needed to install the Markdown formatter. They are not runtime dependencies.
GitHub CLI authentication is needed for live use, not the deterministic tests.

## Style and formatting

Bronson adapts Kapsel's engineering style and pinned Python/Markdown tooling. It does not copy
Kapsel's Rust tooling or product-specific constraints. [ruff.toml](ruff.toml) is the Python settings
owner:

- Python 3.10-compatible syntax, four-space indentation, double quotes, and LF endings;
- a 100-column formatting target, allowing indivisible strings and URLs;
- lint rules `E4`, `E7`, `E9`, `F`, `I`, and `B` for basic errors, unused names, import ordering,
  and likely bugs, not an encyclopedia of style rules;
- Ruff 0.16.6, without preview rules.

Markdown uses Prettier 3.6.2, wrapped prose at 100 columns, and two-space indentation. Tables, URLs,
and code blocks may exceed the prose width. `scripts/format.sh` owns these options and ignores
external Prettier configuration for reproducibility.

```sh
./scripts/format.sh             # format Markdown, then Python
./scripts/format.sh check       # check only, no source rewrites
ruff check --no-cache --config ruff.toml .
```

The formatting script validates both tool versions before writing. Formatting does not fix lint
findings. Inspect proposed fixes and apply only safe, understood changes. Never enable unsafe fixes
just to make the gate pass. Suppressions must be narrow and explained.

Prefer precise names and coherent modules over comments or abstractions that only shorten a file.
Comments explain non-local invariants, compatibility, or why an obvious alternative is wrong.

## Validation

The full local deterministic gate checks formatting, lint, unit and Textual integration tests, and
patch whitespace:

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

The gate runs both directories while extraction is in progress. Move tests into `tests/` as their
owners move into the package. No new test framework is needed.

For documentation changes, also check that local file links and heading anchors resolve and that
planned behavior is not presented as shipped. The gate does not currently automate link checking.

For application changes, manually check affected interactions when possible. Keep live evidence
separate from deterministic tests, and do not rerun or cancel real workflows without authorization.
A passing mocked test does not prove authentication, terminal appearance, or compatibility with a
particular hosted instance.
