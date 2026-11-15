"""Dev tool: python dev/make_icons.py ., renders the brand mark SVG to PNGs with headless Edge (exact SVG rendering) and builds favicon.ico from them."""

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(sys.argv[1])
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BRAND = ROOT / "clippy" / "web" / "static" / "brand"
SIZE = 1024

work = Path(tempfile.mkdtemp())
page = work / "mark.html"
svg = (BRAND / "mark.svg").read_text(encoding="utf-8")
page.write_text(f"<!doctype html><html><body style='margin:0;background:transparent'>"
                f"<div style='width:{SIZE}px;height:{SIZE}px'>{svg.replace('<svg ', f'<svg width=\"{SIZE}\" height=\"{SIZE}\" ', 1)}</div>"
                f"</body></html>", encoding="utf-8")
shot = work / "mark.png"
subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                f"--window-size={SIZE},{SIZE}", f"--screenshot={shot}", page.as_uri()], check=True,
               capture_output=True, timeout=120)
big = cv2.imread(str(shot), cv2.IMREAD_UNCHANGED)
assert big is not None and big.shape[2] == 4, "no alpha"
big = big[:SIZE, :SIZE]


def png(size: int, opaque: bool = False) -> bytes:
    small = cv2.resize(big, (size, size), interpolation=cv2.INTER_AREA)
    if opaque:
        alpha = small[..., 3:4].astype(np.float32) / 255
        background = np.array([0x29, 0x13, 0x6E], np.float32)
        rgb = small[..., :3].astype(np.float32) * alpha + background * (1 - alpha)
        small = rgb.round().astype(np.uint8)
    return cv2.imencode(".png", small, [cv2.IMWRITE_PNG_COMPRESSION, 9])[1].tobytes()


for size, name in ((32, "favicon-32.png"), (192, "icon-192.png"), (512, "icon-512.png")):
    (BRAND / name).write_bytes(png(size))
(BRAND / "apple-touch-icon.png").write_bytes(png(180, opaque=True))

images = [(s, png(s)) for s in (16, 32, 48)]
header = struct.pack("<HHH", 0, 1, len(images))
offset = 6 + 16 * len(images)
entries, blobs = b"", b""
for size, data in images:
    entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset)
    blobs += data
    offset += len(data)
(BRAND / "favicon.ico").write_bytes(header + entries + blobs)
print("corner alpha", big[2, 2, 3], "centre", big[SIZE // 2, SIZE // 2])
