#!/usr/bin/env python3
"""Fetch the Doom status-bar face frames doomface draws over a pane's corner.

The art lives in id Software's own v1.9 shareware IWAD, which id has freely
redistributed since 1994 — the same legal tier as the git-repo sprite rips
already vendored in packs.json's `games` table, and arguably a cleaner one:
this is the vendor's own freeware release rather than a fan recreation.

The IWAD itself is not ours to keep in the repo (4MB, and not this plugin's to
redistribute — same reasoning as sync-assets.sh), so this clones a repo that
mirrors the shareware release, verifies the exact file before trusting it, cuts
the eight face lumps doomface needs out of the WAD's own "picture" format using
its own PLAYPAL, and throws the clone away. Only the decoded RGBA frames and a
manifest of their sizes land in the cache — never the WAD.

Verification is a hash check, not a trust fall: SOURCE_SHA1 is this file's own
SHA-1 as published by wad-archive.com's hash-keyed catalog, cross-checked here
against the freshly cloned copy. A mismatch aborts rather than decodes — a
wrong file decoded without error is a worse failure than no face at all.

usage: fetch-doom-wad.py [--force]
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

# lump name -> nothing; the draw loop keys frames by lump name directly. Each
# is the band's plain look-straight frame (…0), not the turn/ouch/evil-grin
# variants real Doom also has — those are event-triggered (took damage, picked
# up an item) and there is no equivalent discrete event in a token-percentage
# poll, so this sticks to the five health bands plus the two special faces.
LUMP_NAMES = [
    "STFST00", "STFST10", "STFST20", "STFST30", "STFST40",  # bands 0 (calm) .. 4 (worst)
    "STFDEAD0",  # context exhausted
    "STFGOD0",   # fresh session, full context
]


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "herdr-kit" / "doomface" / "frames"


def already_done(dest):
    manifest = dest / "manifest.json"
    if not manifest.exists():
        return False
    try:
        have = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return False
    return all(name in have and (dest / f"{name}.rgba").exists() for name in LUMP_NAMES)


def clone(url, into):
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(into)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def verify(path):
    data = path.read_bytes()
    if data[:4] != b"IWAD":
        raise ValueError(f"{path.name}: not an IWAD (bad magic)")
    digest = hashlib.sha1(data).hexdigest()
    if digest != SOURCE_SHA1:
        raise ValueError(f"{path.name}: sha1 {digest} != expected {SOURCE_SHA1}")
    return data


def read_lumps(data):
    magic, numlumps, infotableofs = struct.unpack_from("<4sii", data, 0)
    lumps = {}
    off = infotableofs
    for _ in range(numlumps):
        filepos, size, raw_name = struct.unpack_from("<ii8s", data, off)
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", "replace")
        lumps[name] = (filepos, size)
        off += 16
    return lumps


def decode_patch(data, lumps, name, palette):
    """Doom "picture" format: width/height/offsets, then per-column posts of
    (topdelta, length, pad, length palette-index bytes, pad), 0xFF topdelta
    ends the column. Vanilla status-bar faces are well under the 254-row
    threshold where source ports special-case "tall patch" columns, so the
    classic non-cumulative decode is exact here."""
    filepos, _size = lumps[name]
    width, height, _left, _top = struct.unpack_from("<hhhh", data, filepos)
    colofs = struct.unpack_from(f"<{width}i", data, filepos + 8)
    buf = bytearray(width * height * 4)  # zeroed = fully transparent
    for x in range(width):
        pos = filepos + colofs[x]
        while True:
            topdelta = data[pos]
            if topdelta == 0xFF:
                break
            length = data[pos + 1]
            pos += 3
            for i in range(length):
                y = topdelta + i
                if 0 <= y < height:
                    r, g, b = palette[data[pos + i]]
                    o = (y * width + x) * 4
                    buf[o:o + 4] = bytes((r, g, b, 255))
            pos += length + 1
    return width, height, bytes(buf)


def main() -> int:
    force = "--force" in sys.argv[1:]
    dest = cache_dir()
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
        palette = [tuple(data[pal_pos + i:pal_pos + i + 3]) for i in range(0, 768, 3)]

        dest.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for name in LUMP_NAMES:
            if name not in lumps:
                print(f"fetch-doom-wad: lump '{name}' missing from this IWAD, skipping", file=sys.stderr)
                continue
            width, height, rgba = decode_patch(data, lumps, name, palette)
            (dest / f"{name}.rgba").write_bytes(rgba)
            manifest[name] = {"width": width, "height": height}

        if not manifest:
            print("fetch-doom-wad: no frames decoded", file=sys.stderr)
            return 1

        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"fetch-doom-wad: {len(manifest)} frames -> {dest}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
