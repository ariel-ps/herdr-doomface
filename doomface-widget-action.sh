#!/usr/bin/env zsh
# herdr action: open the standalone Doom-face widget pane, split off whichever
# pane was active when the action ran — not this action's own pane, which is
# a throwaway shell Herdr spawns just to run this command.
#
# HERDR_ACTIVE_PANE_ID over HERDR_PANE_ID: the former is the pane the user was
# looking at, the latter (if set at all here) would be this action's own
# ephemeral pane. Falling back to it only covers a caller with neither set.

emulate -L zsh

target="${HERDR_ACTIVE_PANE_ID:-${HERDR_PANE_ID:-}}"
if [[ -z "$target" ]]; then
  print -u2 "doomface-widget: no active pane to track"
  exit 1
fi

exec herdr plugin pane open --plugin dev.ariel.herdr-doomface --entrypoint doomface-widget \
  --placement split --target-pane "$target" --direction right --no-focus \
  --env "HERDR_DOOMFACE_TARGET=$target"
