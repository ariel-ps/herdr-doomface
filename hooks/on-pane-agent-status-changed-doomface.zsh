#!/usr/bin/env zsh
# Workers own their locks and check stop requests; this hook never signals PIDs.
emulate -L zsh
script_dir=${0:A:h}
root="${HERDR_PLUGIN_ROOT:-${script_dir:h}}"

if [[ "${1:-}" == --stop-all ]]; then
  exec "$root/libexec/herdr-doomface" --stop-all
fi

command -v jq >/dev/null 2>&1 || exit 0
config="${HERDR_PLUGIN_CONFIG_DIR:-$root}"
[[ -r "$config/config.sh" ]] && source "$config/config.sh"
export HERDR_DOOMFACE_INTERVAL HERDR_DOOMFACE_COLS HERDR_DOOMFACE_ROWS HERDR_DOOMFACE_CORNER HERDR_DOOMFACE_Z
[[ "${HERDR_DOOMFACE_OFF:-0}" == 1 ]] && exit 0

pane=$(print -r -- "${HERDR_PLUGIN_EVENT_JSON:-}" | jq -r '.pane_id // empty' 2>/dev/null)
agent=$(print -r -- "${HERDR_PLUGIN_EVENT_JSON:-}" | jq -r '.agent // empty' 2>/dev/null)
[[ -n "$pane" && "$agent" == claude ]] || exit 0

# Simultaneous events may launch competitors; only the lock owner keeps running.
if command -v uv >/dev/null 2>&1 || python3 -c '' >/dev/null 2>&1; then
  "$root/libexec/herdr-doomface" "$pane" >/dev/null 2>&1 &!
fi
exit 0
