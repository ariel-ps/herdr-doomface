"""Draw a Doom status-bar face in a Claude pane's corner.

The worker polls the pane's transcript for context-window usage, draws through
Herdr's graphics socket, and reports matching sidebar metadata. It exits when
the pane closes, stops being a Claude pane, the plugin is disabled, or a
cooperative stop request is created.
"""

import base64
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .core import (
    FRAME_DEAD,
    FRAME_FOR_BAND,
    FRAME_GOD,
    FrameInfo,
    herdr_bin,
    herdr_pane_get,
    load_frame_bytes,
    load_manifest,
    pick_frame,
    remaining_pct_for_pane,
)

LAYER_ID = "doomface"

# The sidebar has no bitmap layer, so a $doomface token gets an emoji stand-in
# for whichever frame the corner sprite is showing, band for band.
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


def pane_rect(pane_id: str) -> tuple[int, int] | None:
    """Return this pane's width and height in cells, if available."""
    try:
        proc = subprocess.run(
            [herdr_bin(), "pane", "layout", "--pane", pane_id],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        panes = json.loads(proc.stdout)["result"]["layout"]["panes"]
        for pane in panes:
            if pane.get("pane_id") == pane_id:
                rect = pane["rect"]
                return rect["width"], rect["height"]
    except (ValueError, KeyError, TypeError):
        return None
    return None


def corner_offset(
    pane_id: str,
    corner: str,
    cols: int,
    rows: int,
) -> tuple[int, int]:
    rect = pane_rect(pane_id)
    width, height = rect if rect else (cols, rows)
    col = max(0, width - cols) if corner in ("tr", "br") else 0
    row = max(0, height - rows) if corner in ("bl", "br") else 0
    return col, row


def herdr_socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or str(
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        / "herdr" / "herdr.sock"
    )


def rpc(method: str, params: dict[str, Any]) -> bool:
    req = json.dumps({"id": "doomface", "method": method, "params": params}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(herdr_socket_path())
            connection.sendall(req.encode())
            response = bytearray()
            while b"\n" not in response and len(response) <= 1 << 20:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
    except OSError:
        return False
    if b"\n" not in response or len(response) > 1 << 20:
        return False
    try:
        reply = json.loads(response.partition(b"\n")[0])
    except (ValueError, TypeError):
        return False
    return isinstance(reply, dict) and "error" not in reply


def draw(
    pane_id: str,
    name: str,
    manifest: dict[str, FrameInfo],
    col: int,
    row: int,
    cols: int,
    rows: int,
    z: int,
) -> bool:
    info = manifest[name]
    data = load_frame_bytes(name)
    if not data:
        return False
    return rpc("pane.graphics.set", {
        "pane_id": pane_id,
        "format": "rgba",
        "image_width": info["width"],
        "image_height": info["height"],
        "data_base64": base64.b64encode(data).decode("ascii"),
        "layer_id": LAYER_ID,
        "z_index": z,
        "placement": {
            "viewport_col": col, "viewport_row": row,
            "grid_cols": cols, "grid_rows": rows,
        },
    })


def clear(pane_id: str) -> bool:
    return rpc("pane.graphics.clear", {"pane_id": pane_id, "layer_id": LAYER_ID})


def report_tokens(pane_id: str, emoji: str, remaining_pct: float) -> bool:
    # Reporting metadata makes the values available to text-only sidebar rows.
    try:
        result = subprocess.run(
            [herdr_bin(), "pane", "report-metadata", pane_id,
             "--source", METADATA_SOURCE,
             "--token", f"doomface={emoji}",
             "--token", f"doomface_pct={round(remaining_pct * 100)}"],
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
    manifest = load_manifest()
    if not manifest:
        return 0  # no synced frames — nothing to draw, nothing to clear

    interval = float(os.environ.get("HERDR_DOOMFACE_INTERVAL", "4"))
    cols = int(os.environ.get("HERDR_DOOMFACE_COLS", "8"))
    rows = int(os.environ.get("HERDR_DOOMFACE_ROWS", "5"))
    corner = os.environ.get("HERDR_DOOMFACE_CORNER", "tr")
    z = int(os.environ.get("HERDR_DOOMFACE_Z", "5"))

    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    col, row = corner_offset(pane_id, corner, cols, rows)
    last_drawn = None
    last_reported = None

    try:
        while not stop_file.exists() and plugin_enabled():
            pane = herdr_pane_get(pane_id)
            if pane is None or pane.get("agent") != "claude":
                break

            remaining = remaining_pct_for_pane(pane)
            if remaining is not None:
                frame = pick_frame(remaining)
                if frame != last_drawn and frame in manifest:
                    if draw(pane_id, frame, manifest, col, row, cols, rows, z):
                        last_drawn = frame
                reported = (frame, round(remaining * 100))
                if reported != last_reported:
                    if report_tokens(pane_id, EMOJI_FOR_FRAME[frame], remaining):
                        last_reported = reported

            time.sleep(interval)
    finally:
        clear(pane_id)
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
