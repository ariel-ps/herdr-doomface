"""Report a Doom status face for a Claude pane.

The worker polls the pane's transcript for context-window usage and reports
matching sidebar metadata. Bitmap rendering lives in the opt-in widget because
Herdr accepts Kitty graphics only from a process running in its own pane.
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .core import (
    FRAME_DEAD,
    FRAME_FOR_BAND,
    FRAME_GOD,
    herdr_bin,
    herdr_pane_get,
    pick_frame,
    remaining_pct_for_pane,
)

# The sidebar uses an emoji stand-in for each original Doom frame band.
EMOJI_FOR_BAND = ["\U0001F60A", "\U0001F610", "\U0001F61F", "\U0001F630", "\U0001F975"]
EMOJI_DEAD = "\U0001F480"
EMOJI_GOD = "\U0001F607"
EMOJI_FOR_FRAME = dict(zip(FRAME_FOR_BAND, EMOJI_FOR_BAND))
EMOJI_FOR_FRAME[FRAME_DEAD] = EMOJI_DEAD
EMOJI_FOR_FRAME[FRAME_GOD] = EMOJI_GOD
METADATA_SOURCE = "dev.ariel.herdr-doomface:doomface"


def plugin_enabled() -> bool:
    registry = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "herdr/plugins.json"
    try:
        return any(p["plugin_id"] == "dev.ariel.herdr-doomface" and p.get("enabled")
                   for p in json.loads(registry.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def report_tokens(pane_id: str, emoji: str, remaining_pct: float, ttl_ms: int) -> bool:
    # Reporting metadata makes the values available to text-only sidebar rows.
    try:
        result = subprocess.run(
            [herdr_bin(), "pane", "report-metadata", pane_id,
             "--source", METADATA_SOURCE,
             "--token", f"doomface={emoji}",
             "--token", f"doomface_pct={round(remaining_pct * 100)}",
             "--ttl-ms", str(ttl_ms)],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def clear_tokens(pane_id: str) -> bool:
    try:
        result = subprocess.run(
            [herdr_bin(), "pane", "report-metadata", pane_id,
             "--source", METADATA_SOURCE,
             "--clear-token", "doomface",
             "--clear-token", "doomface_pct"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def run(pane_id: str, stop_file: Path) -> int:
    interval = float(os.environ.get("HERDR_DOOMFACE_INTERVAL", "4"))
    ttl_ms = max(1000, round(interval * 3000))

    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
        while not stop_file.exists() and plugin_enabled():
            pane = herdr_pane_get(pane_id)
            if pane is None or pane.get("agent") != "claude":
                break

            remaining = remaining_pct_for_pane(pane)
            if remaining is None:
                clear_tokens(pane_id)
            else:
                frame = pick_frame(remaining)
                report_tokens(pane_id, EMOJI_FOR_FRAME[frame], remaining, ttl_ms)

            time.sleep(interval)
    finally:
        clear_tokens(pane_id)

    return 0


def worker_directory() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "herdr-kit/doomface/workers"


def stop_all() -> int:
    requested = 0
    for path in worker_directory().glob("*.lock"):
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                path.with_suffix(".stop").touch()
                requested += 1
    print(f"doomface: requested stop for {requested} workers (at the next poll)")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--stop-all"]:
        return stop_all()
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9:_-]+", sys.argv[1]):
        print("usage: herdr-doomface <pane-id> | --stop-all", file=sys.stderr)
        return 2
    pane_id = sys.argv[1]
    directory = worker_directory()
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the lock inode: unlinking it would let a new worker bypass a holder.
    with (directory / f"{pane_id}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stop_file = directory / f"{pane_id}.stop"
        stop_file.unlink(missing_ok=True)
        try:
            return run(pane_id, stop_file)
        finally:
            stop_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
