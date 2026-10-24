#!/usr/bin/env sh
set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

version=$(pyright --version 2>/dev/null) || {
  printf '%s\n' 'typecheck: Pyright 1.1.411 is required' >&2
  exit 1
}
if [ "$version" != 'pyright 1.1.411' ]; then
  printf '%s\n' "typecheck: Pyright 1.1.411 is required; found $version" >&2
  exit 1
fi

# Resolve the same interpreter used by tests, including its installed dependencies.
interpreter=$("${PYTHON:-python3}" -c 'import sys; print(sys.executable)')
pyright --project pyrightconfig.json --pythonpath "$interpreter"
