"""Render Doomface full-size in a dedicated Herdr split pane."""

import base64
import os
import signal
import sys
import time
from types import FrameType

from .core import (
    herdr_pane_get,
    load_frame_bytes,
    load_manifest,
    pick_frame,
    remaining_pct_for_pane,
)

CELL_W = 8
CELL_H = 16
IMAGE_ID = 9100

ESC = "\033"
CSI = "\033["


def term_size_px() -> tuple[int, int]:
    size = os.get_terminal_size()
    return max(1, size.columns - 2) * CELL_W, max(1, size.lines - 2) * CELL_H


def fit(sw: int, sh: int, max_w: int, max_h: int) -> tuple[int, int]:
    scale = max(1, int(min(max_w / sw, max_h / sh)))
    return sw * scale, sh * scale


def scale_nearest(
    data: bytes,
    sw: int,
    sh: int,
    dw: int,
    dh: int,
) -> bytes:
    out = bytearray(dw * dh * 4)
    for y in range(dh):
        sy = min(sh - 1, y * sh // dh)
        src_row = sy * sw * 4
        dst_row = y * dw * 4
        for x in range(dw):
            sx = min(sw - 1, x * sw // dw)
            out[dst_row + x * 4:dst_row + x * 4 + 4] = data[src_row + sx * 4:src_row + sx * 4 + 4]
    return bytes(out)


def send_image(data: bytes, width: int, height: int) -> None:
    b64 = base64.b64encode(data).decode("ascii")
    chunks = [b64[i:i + 4000] for i in range(0, len(b64), 4000)]
    out = [f"{ESC}_Ga=d,d=I,i={IMAGE_ID},q=2{ESC}\\", f"{CSI}2J{CSI}H"]
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        ctrl = f"a=T,f=32,s={width},v={height},i={IMAGE_ID},q=2" if index == 0 else f"i={IMAGE_ID},q=2"
        out.append(f"{ESC}_G{ctrl},m={more};{chunk}{ESC}\\")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def clear_image() -> None:
    sys.stdout.write(f"{ESC}_Ga=d,d=I,i={IMAGE_ID},q=2{ESC}\\")
    sys.stdout.flush()


def show_message(text: str) -> None:
    sys.stdout.write(f"{CSI}2J{CSI}H{text}\n")
    sys.stdout.flush()


def main() -> int:
    target = os.environ.get("HERDR_DOOMFACE_TARGET")
    if not target:
        print("herdr-doomface-widget: HERDR_DOOMFACE_TARGET not set", file=sys.stderr)
        return 2

    interval = float(os.environ.get("HERDR_DOOMFACE_INTERVAL", "4"))
    stop = False

    def on_term(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)

    manifest = load_manifest()
    if not manifest:
        show_message("doomface: no frames cached (run the plugin's asset download step)")
        return 0

    last_frame = None
    last_size = None

    try:
        while not stop:
            pane = herdr_pane_get(target)
            if pane is None or pane.get("agent") != "claude":
                show_message(f"doomface: {target} is gone")
                break

            remaining = remaining_pct_for_pane(pane)
            if remaining is not None:
                frame = pick_frame(remaining)
                size = term_size_px()
                if frame != last_frame or size != last_size:
                    info = manifest.get(frame)
                    data = load_frame_bytes(frame) if info else None
                    if info and data:
                        dw, dh = fit(info["width"], info["height"], *size)
                        scaled = scale_nearest(data, info["width"], info["height"], dw, dh)
                        send_image(scaled, dw, dh)
                        last_frame, last_size = frame, size

            for _ in range(max(1, int(interval * 10))):
                if stop:
                    break
                time.sleep(0.1)
    finally:
        clear_image()

    return 0


if __name__ == "__main__":
    sys.exit(main())
