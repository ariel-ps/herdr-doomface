#!/usr/bin/env zsh
# herdr event hook: keep one bin/herdr-doomface loop running per Claude pane,
# drawing its corner face from that session's own token usage.
#
# pane.agent_status_changed fires on every status change — idle, working,
# blocked, done — not just the first time a pane appears, so this is
# spawn-if-not-already-running rather than spawn-unconditionally: a pidfile
# per pane under XDG cache remembers what this kit already started there, and
# a dead pid found in one (the herdr server restarted, the loop crashed) is
# just replaced rather than treated as still running.
#
# The loop backgrounds and disowns itself (`&!`, same idiom alert-hook.sh uses
# for its flash) so this hook — which herdr waits on — returns immediately. It
# runs until the pane closes or stops being a Claude agent, clearing its own
# overlay before exiting; there is no matching "pane closed" event for this
# hook to clean up from the other end.
#
# Env in: HERDR_PLUGIN_EVENT_JSON (pane_id, agent_status, agent, display_agent).
#   HERDR_DOOMFACE_OFF=1   never start the face
#   (HERDR_DOOMFACE_INTERVAL / _COLS / _ROWS / _CORNER / _Z are read by the
#    loop itself, not this hook — export them from config.sh same as the
#    HERDR_ALERT_* / HERDR_TITLE_* knobs.)

emulate -L zsh

# Double duty: the event path below, and `doomface-stop-all`'s action entry.
# One file for both rather than a second script that would just re-derive the
# same pidfile-directory convention.
if [[ "${1:-}" == "--stop-all" ]]; then
  cache="${XDG_CACHE_HOME:-$HOME/.cache}/herdr-kit/doomface"
  n=0
  for f in "$cache"/pids/*.pid(N); do
    pid="$(<"$f")"
    [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null && (( n++ ))
    rm -f "$f"
  done
  print -r -- "doomface: stopped $n"
  exit 0
fi

# HERDR_PLUGIN_ROOT is set by herdr; the ${0:A:h} fallback keeps the hook
# runnable by hand.
root="${HERDR_PLUGIN_ROOT:-${0:A:h}}"
command -v jq >/dev/null 2>&1 || exit 0

config="${HERDR_PLUGIN_CONFIG_DIR:-$root}"
[[ -r "$config/config.sh" ]] && source "$config/config.sh"
export HERDR_DOOMFACE_INTERVAL HERDR_DOOMFACE_COLS HERDR_DOOMFACE_ROWS HERDR_DOOMFACE_CORNER HERDR_DOOMFACE_Z

[[ "${HERDR_DOOMFACE_OFF:-0}" == 1 ]] && exit 0

pane=$(print -r -- "${HERDR_PLUGIN_EVENT_JSON:-}" | jq -r '.pane_id // empty' 2>/dev/null)
agent=$(print -r -- "${HERDR_PLUGIN_EVENT_JSON:-}" | jq -r '.agent // empty' 2>/dev/null)

# Only Claude panes have a transcript in the shape the loop reads.
[[ -n "$pane" && "$agent" == "claude" ]] || exit 0

cache="${XDG_CACHE_HOME:-$HOME/.cache}/herdr-kit/doomface"
mkdir -p "$cache/pids" 2>/dev/null

# Pane ids carry a colon (w4:pC); a bare filename can't.
pidfile="$cache/pids/${pane//:/_}.pid"

if [[ -r "$pidfile" ]]; then
  existing="$(<"$pidfile")"
  if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
    exit 0   # already tracking this pane
  fi
  rm -f "$pidfile"
fi

# uv first, for the reason __herdr_py in shell/herdr.sh gives: a shim earlier
# on PATH can answer a `python3` lookup and then refuse the call.
if command -v uv >/dev/null 2>&1; then
  uv run --no-project python "$root/bin/herdr-doomface" "$pane" >/dev/null 2>&1 &!
elif python3 -c '' >/dev/null 2>&1; then
  python3 "$root/bin/herdr-doomface" "$pane" >/dev/null 2>&1 &!
else
  exit 0   # no interpreter — no face, not a failed hook
fi

print -r -- "$!" > "$pidfile"

exit 0
