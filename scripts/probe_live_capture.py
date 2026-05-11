"""Mirror the live capture pipeline and dump annotated frames.

For each of N captures we save a thumbnail of the grabbed display
overlaid with the per-LED regions: each rectangle is drawn in the
exact RGB that the live code would have sent to that LED. So a glance
at the PNG answers two questions at once:

1. Does the captured display match what was actually on screen at that
   moment? (i.e. is CGDisplay giving us fresh frames?)
2. Does each LED region pull a sensible colour from underneath it?
   (i.e. is our averaging / channel order correct?)
"""

from __future__ import annotations

import time

import numpy as np
from PIL import Image, ImageDraw

from ambilight.capture import CaptureConfig, ScreenCapture

cap = ScreenCapture(CaptureConfig(depth_fraction=0.15))
print(f"Backend: {type(cap.backend).__name__}, "
      f"display {cap.screen_w}×{cap.screen_h}")

N = 5
INTERVAL = 1.0  # seconds between captures so you can move things on screen

for i in range(N):
    t0 = time.monotonic()
    bgra = cap.backend.grab()
    grab_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    colors = cap.grab_colors()  # 2nd grab inside ScreenCapture, plus averaging
    full_ms = (time.monotonic() - t0) * 1000

    # Compose annotated frame
    h, w = bgra.shape[:2]
    img = Image.frombytes("RGB", (w, h), bgra[..., [2, 1, 0]].tobytes())
    draw = ImageDraw.Draw(img)
    for led_idx, (l, t, r, b) in enumerate(cap.regions):
        c = tuple(int(x) for x in colors[led_idx])
        # filled rectangle of the averaged colour so it stands out
        draw.rectangle([l, t, r, b], fill=c)
        draw.rectangle([l, t, r, b], outline=(255, 255, 255), width=3)
    img.thumbnail((1400, 1400))
    out = f"/tmp/ambilight_live_{i:02d}.png"
    img.save(out)

    # Print a few sampled LEDs so you can correlate with the PNG
    samples = [(0, "LED  0 right-bottom"),
               (8, "LED  8 right-mid"),
               (16, "LED 16 right-top"),
               (32, "LED 32 top-middle"),
               (47, "LED 47 top-left"),
               (56, "LED 56 left-mid"),
               (64, "LED 64 left-bottom")]
    print(f"\nFrame {i+1}/{N}  grab={grab_ms:.1f}ms  full_pipeline={full_ms:.1f}ms"
          f"  saved {out}")
    for idx, name in samples:
        r, g, b_ = colors[idx]
        print(f"  {name:>22s}: RGB({r:>3d},{g:>3d},{b_:>3d})")

    if i < N - 1:
        time.sleep(INTERVAL)

cap.close()
print(f"\nOpen /tmp/ambilight_live_*.png and check:")
print("  • does each PNG look like your screen at that moment? (fresh capture?)")
print("  • do the coloured rectangles match the pixels they overlap?")
