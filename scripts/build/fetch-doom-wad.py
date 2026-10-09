#!/usr/bin/env python3
"""Fetch and decode the Doom status-bar face frames used by Doomface.

The shareware IWAD is cloned into a temporary directory, verified against its
known SHA-1, decoded into cached RGBA frames, and then discarded.

Usage: fetch-doom-wad.py [--force]
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_REPO = "https://github.com/Doom-Utils/shareware-collection.git"
SOURCE_PATH = "Doom 1.9/doom1.wad"
SOURCE_SHA1 = "5b2e249b9c5133ec987b3ea77596381dc0d6bc1d"

# Each entry is the band's plain look-straight frame, plus the two special
# faces. Event-triggered turn/ouch/grin variants do not map to token polling.
LUMP_NAMES = [
    "STFST00", "STFST10", "STFST20", "STFST30", "STFST40",
    "STFDEAD0",
    "STFGOD0",
]


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "herdr-kit" / "doomface"


def cache_dir(root: Path) -> Path:
    current = root / "current"
    try:
        if not current.is_symlink():
            name = current.read_text().strip()
            if name.startswith("frames-") and name.replace("-", "").isalnum():
                candidate = root / "sets" / name
                if candidate.is_dir() and not candidate.is_symlink():
                    return candidate
    except OSError:
        pass
    return root / "frames"


def already_done(dest: Path) -> bool:
    if not dest.is_dir() or dest.is_symlink():
        return False
    manifest = dest / "manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    try:
        have = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(have, dict) or set(have) != set(LUMP_NAMES):
        return False
    for name in LUMP_NAMES:
        info = have.get(name)
        if not isinstance(info, dict):
            return False
        width, height = info.get("width"), info.get("height")
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            return False
        frame = dest / f"{name}.rgba"
        try:
            if frame.is_symlink() or frame.stat().st_size != width * height * 4:
                return False
        except OSError:
            return False
    return True


def publish_cache(staged: Path, root: Path) -> None:
    """Atomically point readers at a fully written versioned cache."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as pointer:
            pointer.write(f"{staged.name}\n")
            pointer.flush()
            os.fsync(pointer.fileno())
        os.replace(temporary_name, root / "current")
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def clone(url: str, into: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(into)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def verify(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] != b"IWAD":
        raise ValueError(f"{path.name}: not an IWAD (bad magic)")
    digest = hashlib.sha1(data).hexdigest()
    if digest != SOURCE_SHA1:
        raise ValueError(f"{path.name}: sha1 {digest} != expected {SOURCE_SHA1}")
    return data


def read_lumps(data: bytes) -> dict[str, tuple[int, int]]:
    _magic, numlumps, infotableofs = struct.unpack_from("<4sii", data, 0)
    lumps = {}
    offset = infotableofs
    for _ in range(numlumps):
        filepos, size, raw_name = struct.unpack_from("<ii8s", data, offset)
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", "replace")
        lumps[name] = (filepos, size)
        offset += 16
    return lumps


def decode_patch(
    data: bytes,
    lumps: dict[str, tuple[int, int]],
    name: str,
    palette: list[tuple[int, int, int]],
) -> tuple[int, int, bytes]:
    """Decode one classic Doom picture lump into transparent RGBA bytes."""
    filepos, _size = lumps[name]
    width, height, _left, _top = struct.unpack_from("<hhhh", data, filepos)
    colofs = struct.unpack_from(f"<{width}i", data, filepos + 8)
    buf = bytearray(width * height * 4)
    for x in range(width):
        pos = filepos + colofs[x]
        while True:
            topdelta = data[pos]
            if topdelta == 0xFF:
                break
            length = data[pos + 1]
            pos += 3
            for index in range(length):
                y = topdelta + index
                if 0 <= y < height:
                    red, green, blue = palette[data[pos + index]]
                    output_offset = (y * width + x) * 4
                    buf[output_offset:output_offset + 4] = bytes((red, green, blue, 255))
            pos += length + 1
    return width, height, bytes(buf)


def main() -> int:
    force = "--force" in sys.argv[1:]
    root = cache_root()
    dest = cache_dir(root)
    if not force and already_done(dest):
        print(f"fetch-doom-wad: {len(LUMP_NAMES)} frames already cached", file=sys.stderr)
        return 0

    if not shutil.which("git"):
        print("fetch-doom-wad: git not found", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "shareware-collection"
        try:
            clone(SOURCE_REPO, clone_dir)
        except subprocess.CalledProcessError as exc:
            print(f"fetch-doom-wad: clone failed: {exc}", file=sys.stderr)
            return 1

        wad_path = clone_dir / SOURCE_PATH
        if not wad_path.exists():
            print(f"fetch-doom-wad: {SOURCE_PATH} not found in clone", file=sys.stderr)
            return 1

        try:
            data = verify(wad_path)
        except ValueError as exc:
            print(f"fetch-doom-wad: {exc} — refusing to use an unverified WAD", file=sys.stderr)
            return 1

        lumps = read_lumps(data)
        if "PLAYPAL" not in lumps:
            print("fetch-doom-wad: no PLAYPAL lump", file=sys.stderr)
            return 1
        pal_pos, _pal_size = lumps["PLAYPAL"]
        palette = [tuple(data[pal_pos + index:pal_pos + index + 3]) for index in range(0, 768, 3)]

        missing = [name for name in LUMP_NAMES if name not in lumps]
        if missing:
            print(
                f"fetch-doom-wad: required lumps missing: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 1

        sets = root / "sets"
        sets.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix="frames-", dir=sets))
        try:
            manifest = {}
            for name in LUMP_NAMES:
                width, height, rgba = decode_patch(data, lumps, name, palette)
                (staged / f"{name}.rgba").write_bytes(rgba)
                manifest[name] = {"width": width, "height": height}
            (staged / "manifest.json").write_text(json.dumps(manifest, indent=2))
            publish_cache(staged, root)
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise

        print(f"fetch-doom-wad: {len(manifest)} frames -> {staged}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
