"""Dev tool: python dev/make_og.py . – renders dev/og.html to docs/assets/og.png (1200×630) with headless Edge."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
out = ROOT / "docs" / "assets" / "og.png"
subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1200,630",
                f"--screenshot={out}", (ROOT / "dev" / "og.html").as_uri()], check=True, capture_output=True, timeout=120)
print(out, out.stat().st_size)
