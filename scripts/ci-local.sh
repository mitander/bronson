#!/usr/bin/env sh
set -eu

# Keep gate output machine-readable. ANSI sequences make log viewers reject the response.
export NO_COLOR=1
export FORCE_COLOR=0

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

./scripts/format.sh check
shfmt -f=0 . | xargs -0 shellcheck -x
ruff check --no-cache --config ruff.toml .
./scripts/typecheck.sh
"${PYTHON:-python3}" -m unittest discover -s tests -p 'test_*.py'
git diff --check
