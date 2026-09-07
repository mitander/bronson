#!/usr/bin/env sh
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

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

# Check every formatter before any source is rewritten.
prettier_version=$(prettier --version 2>/dev/null) || {
  printf '%s\n' "format: Prettier 3.6.2 is required" >&2
  exit 1
}
if [ "$prettier_version" != "3.6.2" ]; then
  printf '%s\n' "format: Prettier 3.6.2 is required; found $prettier_version" >&2
  exit 1
fi
ruff check --no-cache --config ruff.toml --show-settings bronson/cli.py >/dev/null

prettier "$prettier_mode" --no-config --ignore-path .gitignore --print-width 100 \
  --prose-wrap always --tab-width 2 '**/*.md'
ruff format --no-cache --config ruff.toml ${check_mode:+"$check_mode"} .
