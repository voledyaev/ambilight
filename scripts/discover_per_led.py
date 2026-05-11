"""Per-LED diagnostic: paint each side a distinct hue with a brightness
ramp along its direction. Confirms side colours, side LED counts, and
LED-index direction in one viewing.

Expected (if our geometry model in `ambilight.geometry` is correct):

  - LED 0 (bottom-right corner): WHITE
  - LEDs 1..16 (right side, going UP):  red, getting brighter toward the top
  - LEDs 17..47 (top side, going LEFT):  green, getting brighter toward the left
  - LEDs 48..64 (left side, going DOWN): blue, getting brighter toward the bottom

So walking the strip from start to end you should see, in order:
white → dim red → bright red → dim green → bright green → dim blue → bright blue.

If brightness goes the OTHER way on any side, the geometry's `direction`
field for that side is reversed and we'll flip it.

If a side comes out the wrong colour, the side mapping is wrong.
"""

from __future__ import annotations

import time

from ambilight.device import AmbilightDevice
from ambilight.geometry import LEFT, NUM_LEDS, RIGHT, TOP

DIM = 24
BRIGHT = 200


def ramp(idx_in_side: int, side_count: int) -> int:
    """Linear brightness from DIM (at idx=0) up to BRIGHT (at idx=count-1)."""
    if side_count <= 1:
        return BRIGHT
    return DIM + (BRIGHT - DIM) * idx_in_side // (side_count - 1)


def build_palette() -> list[tuple[int, int, int]]:
    colors: list[tuple[int, int, int]] = [(0, 0, 0)] * NUM_LEDS

    colors[0] = (80, 80, 80)  # LED 0 → white marker

    # Right side: LEDs 1..16 → red, brightness increases as i grows.
    # Side LEDs in geometry.RIGHT: first_led=0 (white), so LEDs 1..16 are
    # indices 1..16 within the side (16 LEDs total in this gradient).
    for global_i in range(RIGHT.first_led + 1, RIGHT.first_led + RIGHT.count):
        local_i = global_i - (RIGHT.first_led + 1)
        colors[global_i] = (ramp(local_i, RIGHT.count - 1), 0, 0)

    # Top side: green, brightness increases toward the left end.
    for global_i in range(TOP.first_led, TOP.first_led + TOP.count):
        local_i = global_i - TOP.first_led
        colors[global_i] = (0, ramp(local_i, TOP.count), 0)

    # Left side: blue, brightness increases toward the bottom.
    for global_i in range(LEFT.first_led, LEFT.first_led + LEFT.count):
        local_i = global_i - LEFT.first_led
        colors[global_i] = (0, 0, ramp(local_i, LEFT.count))

    return colors


def main() -> int:
    palette = build_palette()

    print("Per-LED diagnostic palette:")
    print("  LED 0:        WHITE (80,80,80)")
    print(f"  LEDs 1..16:   RED ramp {DIM} → {BRIGHT} (right side, brighter at top)")
    print(f"  LEDs 17..47:  GREEN ramp {DIM} → {BRIGHT} (top side, brighter at left)")
    print(f"  LEDs 48..64:  BLUE ramp {DIM} → {BRIGHT} (left side, brighter at bottom)")
    print(f"\nHolding this frame for 15 s at ~60 Hz...\n")

    with AmbilightDevice() as dev:
        deadline = time.monotonic() + 15.0
        frames = 0
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            dev.send_leds(palette)
            frames += 1
            time.sleep(1.0 / 60.0)
        elapsed = time.monotonic() - t0
        print(f"Sent {frames} frames in {elapsed:.2f} s ({frames / elapsed:.1f} fps).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
