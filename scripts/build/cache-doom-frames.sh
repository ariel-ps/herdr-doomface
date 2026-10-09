#!/bin/sh

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
root=${HERDR_PLUGIN_ROOT:-$(dirname -- "$(dirname -- "$script_dir")")}
fetcher="$root/scripts/build/fetch-doom-wad.py"

if command -v uv >/dev/null 2>&1; then
  uv run --no-project python "$fetcher"
else
  python3 "$fetcher"
fi || echo "doomface: no frames cached, bitmap widget stays off"
