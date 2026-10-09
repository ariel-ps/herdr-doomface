"""Shared Doomface frame selection and transcript lookup logic."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, TypedDict

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
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class FrameInfo(TypedDict):
    width: int
    height: int


def frames_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    root = Path(base) / "herdr-kit" / "doomface"
    current = root / "current"
    try:
        if not current.is_symlink():
            name = current.read_text().strip()
            if re.fullmatch(r"frames-[A-Za-z0-9_-]+", name):
                candidate = root / "sets" / name
                if candidate.is_dir() and not candidate.is_symlink():
                    return candidate
    except OSError:
        pass
    return root / "frames"


def load_manifest() -> dict[str, FrameInfo] | None:
    manifest = frames_dir() / "manifest.json"
    try:
        value = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    manifest_value: dict[str, FrameInfo] = {}
    for name, info in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(info, dict)
            or not isinstance(info.get("width"), int)
            or not isinstance(info.get("height"), int)
        ):
            return None
        manifest_value[name] = {
            "width": info["width"],
            "height": info["height"],
        }
    return manifest_value


def load_frame_bytes(name: str) -> bytes | None:
    try:
        return (frames_dir() / f"{name}.rgba").read_bytes()
    except OSError:
        return None


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_pane_get(pane_id: str) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            [herdr_bin(), "pane", "get", pane_id],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        pane = json.loads(proc.stdout)["result"]["pane"]
    except (ValueError, KeyError, TypeError):
        return None
    return pane if isinstance(pane, dict) else None


def transcript_path(cwd: str, session_id: str) -> Path:
    # Mirrors Claude Code's own project-directory naming: cwd with every '/'
    # and '.' replaced by '-'. Verified directly against a live transcript
    # path rather than assumed.
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid Claude session ID")
    slug = "".join("-" if c in "/." else c for c in cwd)
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "projects" / slug / f"{session_id}.jsonl"


def last_usage(path: Path) -> tuple[int, int, int, str | None] | None:
    """Return the latest usage snapshot without reading the whole transcript."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    chunk = 65536
    with open(path, "rb") as transcript:
        while True:
            read_size = min(chunk, size)
            transcript.seek(size - read_size)
            data = transcript.read(read_size)
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
                if not isinstance(obj, dict):
                    continue
                message = obj.get("message") or {}
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                input_tokens = usage.get("input_tokens", 0)
                cache_creation = usage.get("cache_creation_input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                model = message.get("model")
                if (
                    type(input_tokens) is not int
                    or type(cache_creation) is not int
                    or type(cache_read) is not int
                    or (model is not None and not isinstance(model, str))
                ):
                    continue
                return (
                    input_tokens,
                    cache_creation,
                    cache_read,
                    model,
                )
            if read_size >= size:
                return None
            chunk *= 4


def context_window_for(model: object) -> int:
    if isinstance(model, str):
        for prefix, window in CONTEXT_WINDOWS.items():
            if model.startswith(prefix):
                return window
    return DEFAULT_CONTEXT_WINDOW


def pick_frame(remaining_pct: float) -> str:
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


def remaining_pct_for_pane(pane: dict[str, Any]) -> float | None:
    """Return ``None`` when the pane has no readable usage snapshot yet."""
    cwd = pane.get("cwd") or pane.get("foreground_cwd")
    agent_session = pane.get("agent_session")
    session_id = agent_session.get("value") if isinstance(agent_session, dict) else None
    if not isinstance(cwd, str) or not isinstance(session_id, str):
        return None
    if not cwd or not SESSION_ID_RE.fullmatch(session_id):
        return None
    usage = last_usage(transcript_path(cwd, session_id))
    if not usage:
        return None
    input_t, cache_creation, cache_read, model = usage
    load = input_t + cache_creation + cache_read
    window = context_window_for(model)
    return max(0.0, 1.0 - load / window)
