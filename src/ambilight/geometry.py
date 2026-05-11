"""Physical layout of the strip on the user's monitor.

This describes which LED index sits on which side and which physical
position. Used by both the diagnostic scripts and the screen-capture
sampling code to choose pixel regions per LED.

Origin / direction convention (looking at the front of the screen):
- LED 0 is at the bottom-right corner where the USB wire enters
- the strip runs counterclockwise: up the right edge, across the top
  right-to-left, then down the left edge to the bottom-left corner
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Side:
    name: str
    first_led: int
    count: int
    axis: str           # "vertical" or "horizontal"
    direction: str      # "up" / "down" / "left" / "right" — the way LED indices advance along the side

    @property
    def last_led(self) -> int:
        return self.first_led + self.count - 1


RIGHT = Side(name="right", first_led=0,  count=17, axis="vertical",   direction="up")
TOP   = Side(name="top",   first_led=17, count=31, axis="horizontal", direction="left")
LEFT  = Side(name="left",  first_led=48, count=17, axis="vertical",   direction="down")

SIDES: tuple[Side, ...] = (RIGHT, TOP, LEFT)
NUM_LEDS = sum(s.count for s in SIDES)  # 65


def side_for_led(led_index: int) -> Side:
    for s in SIDES:
        if s.first_led <= led_index <= s.last_led:
            return s
    raise IndexError(f"LED {led_index} is outside the strip ({NUM_LEDS} LEDs total)")


def position_on_side(led_index: int) -> tuple[Side, int]:
    """Return (side, index_within_side) for a global LED index."""
    s = side_for_led(led_index)
    return s, led_index - s.first_led
