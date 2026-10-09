#!/usr/bin/env zsh
# Open the standalone Doomface widget beside the pane active for this action.
emulate -L zsh

target="${HERDR_ACTIVE_PANE_ID:-${HERDR_PANE_ID:-}}"
if [[ -z "$target" ]]; then
  print -u2 "doomface-widget: no active pane to track"
  exit 1
fi

exec "${HERDR_BIN_PATH:-herdr}" plugin pane open --plugin dev.ariel.herdr-doomface --entrypoint doomface-widget \
  --placement split --target-pane "$target" --direction right --no-focus \
  --env "HERDR_DOOMFACE_TARGET=$target"
