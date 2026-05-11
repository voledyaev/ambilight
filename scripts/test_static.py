"""Smoke test: light the whole strip with one solid colour using the
production protocol.

If colour switches happen instantly (no ~5 s fade) this is end-to-end
proof that the project is in working order.

Usage:
    uv run python scripts/test_static.py            # default cycle
    uv run python scripts/test_static.py red
    uv run python scripts/test_static.py 255 0 128
"""

from __future__ import annotations

import sys
import time

from ambilight.device import AmbilightDevice

NAMED = {
    "off":    (0, 0, 0),
    "red":    (96, 0, 0),
    "green":  (0, 96, 0),
    "blue":   (0, 0, 96),
    "white":  (64, 64, 64),
    "yellow": (96, 96, 0),
    "cyan":   (0, 96, 96),
    "purple": (96, 0, 96),
}


def parse_color(argv: list[str]) -> tuple[int, int, int] | None:
    if not argv:
        return None
    if len(argv) == 1 and argv[0].lower() in NAMED:
        return NAMED[argv[0].lower()]
    if len(argv) == 3:
        return tuple(int(x) for x in argv)  # type: ignore[return-value]
    raise SystemExit(
        f"usage: {sys.argv[0]} [color_name | R G B]\n"
        f"named: {', '.join(NAMED)}"
    )


def cycle(dev: AmbilightDevice) -> None:
    """Default behaviour: walk through a few solid colours so the user
    can confirm instant switching."""
    for name in ("red", "green", "blue", "yellow", "purple", "off"):
        color = NAMED[name]
        print(f"  → {name:<7} {color}")
        for _ in range(15):  # ~1 s at the inter-chunk-paced 15 Hz
            dev.send_leds([color] * 65)


def main() -> int:
    color = parse_color(sys.argv[1:])
    with AmbilightDevice() as dev:
        if color is None:
            cycle(dev)
        else:
            print(f"Painting strip {color} for 5 s.")
            t_end = time.monotonic() + 5.0
            while time.monotonic() < t_end:
                dev.send_leds([color] * 65)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
