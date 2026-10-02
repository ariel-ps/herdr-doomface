"""Shared logic between herdr-doomface (corner overlay) and
herdr-doomface-widget (dedicated split pane): which frame to show for a
Claude pane's current context-window usage, and how to find that pane's own
transcript. Not an entrypoint — nothing here talks to a pane's own tty or the
graphics socket, callers do that themselves since the two entrypoints draw in
completely different ways (RPC into someone else's pane vs. escape codes
into their own).
"""

import json
import os
import subprocess
from pathlib import Path

# Straight-ahead frame for each health band (0 = calm .. 4 = worst), plus the
# two special faces. Real Doom also has turn/ouch/evil-grin variants per band,
# but those are event-triggered (took damage, picked up an item) and a token
# poll has no equivalent discrete event to hang them on.
FRAME_FOR_BAND = ["STFST00", "STFST10", "STFST20", "STFST30", "STFST40"]
FRAME_DEAD = "STFDEAD0"
FRAME_GOD = "STFGOD0"

# All current Claude models share this context window; the table exists so a
# future model with a different one is a lookup away, not a rewrite.
CONTEXT_WINDOWS = {
    "claude-opus-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000


def frames_dir():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "herdr-kit" / "doomface" / "frames"


def load_manifest():
    manifest = frames_dir() / "manifest.json"
    try:
        return json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None


def load_frame_bytes(name):
    try:
        return (frames_dir() / f"{name}.rgba").read_bytes()
    except OSError:
        return None


def herdr_pane_get(pane_id):
    try:
        proc = subprocess.run(
            ["herdr", "pane", "get", pane_id],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)["result"]["pane"]
    except (ValueError, KeyError, TypeError):
        return None


def transcript_path(cwd, session_id):
    # Mirrors Claude Code's own project-directory naming: cwd with every '/'
    # and '.' replaced by '-'. Verified directly against a live transcript
    # path rather than assumed.
    slug = "".join("-" if c in "/." else c for c in cwd)
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def last_usage(path):
    """The most recent usage snapshot in the transcript, scanning backward in
    growing chunks rather than reading the whole file — a long-running
    session's transcript can be many MB, most of it tool output appended long
    after the last turn that actually reports usage."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    chunk = 65536
    with open(path, "rb") as f:
        while True:
            read_size = min(chunk, size)
            f.seek(size - read_size)
            data = f.read(read_size)
            lines = data.split(b"\n")
            if read_size < size:
                lines = lines[1:]  # first line may be a truncated partial
            for line in reversed(lines):
                if b'"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") or {}
                usage = message.get("usage")
                if not usage:
                    continue
                return (
                    usage.get("input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    message.get("model"),
                )
            if read_size >= size:
                return None
            chunk *= 4


def context_window_for(model):
    if isinstance(model, str):
        for prefix, window in CONTEXT_WINDOWS.items():
            if model.startswith(prefix):
                return window
    return DEFAULT_CONTEXT_WINDOW


def pick_frame(remaining_pct):
    if remaining_pct <= 0.02:
        return FRAME_DEAD
    if remaining_pct >= 0.98:
        return FRAME_GOD
    band = 0
    for threshold in (0.80, 0.60, 0.40, 0.20):
        if remaining_pct >= threshold:
            break
        band += 1
    else:
        band = 4
    return FRAME_FOR_BAND[band]


def remaining_pct_for_pane(pane):
    """None if this pane has nothing to read yet (no cwd/session, no usage
    line seen so far) — callers treat that as "nothing to draw", not zero."""
    cwd = pane.get("cwd") or pane.get("foreground_cwd")
    session_id = (pane.get("agent_session") or {}).get("value")
    if not (cwd and session_id):
        return None
    usage = last_usage(transcript_path(cwd, session_id))
    if not usage:
        return None
    input_t, cache_creation, cache_read, model = usage
    load = input_t + cache_creation + cache_read
    window = context_window_for(model)
    return max(0.0, 1.0 - load / window)
