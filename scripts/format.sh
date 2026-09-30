#!/usr/bin/env sh
set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

case "${1:-write}" in
write)
  prettier_mode=--write
  check_mode=
  ;;
check | --check)
  prettier_mode=--check
  check_mode=--check
  ;;
*)
  printf '%s\n' "usage: $0 [write|check]" >&2
  exit 2
  ;;
esac

prettier_version=$(prettier --version 2>/dev/null) || {
  printf '%s\n' "format: Prettier 3.9.6 is required" >&2
  exit 1
}
if [ "$prettier_version" != "3.9.6" ]; then
  printf '%s\n' "format: Prettier 3.9.6 is required; found $prettier_version" >&2
  exit 1
fi
ruff check --no-cache --config ruff.toml --show-settings bronson/cli.py >/dev/null
shfmt_version=$(shfmt --version)
[ "${shfmt_version#v}" = '3.14.1' ]
shellcheck --version | grep -qx 'version: 0.11.0'
command -v taplo >/dev/null

prettier "$prettier_mode" --ignore-path .gitignore '**/*.{md,json,jsonc,yaml,yml}'
if [ -n "$check_mode" ]; then
  ruff check --no-cache --config ruff.toml --select I .
else
  ruff check --no-cache --config ruff.toml --select I --fix .
fi
ruff format --no-cache --config ruff.toml ${check_mode:+"$check_mode"} .
git ls-files -z --cached --others --exclude-standard '*.toml' |
  xargs -0 taplo fmt ${check_mode:+"$check_mode"}
if [ -n "$check_mode" ]; then
  shfmt -i 2 -d .
else
  shfmt -i 2 -w .
fi
