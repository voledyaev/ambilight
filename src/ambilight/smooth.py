"""Temporal smoothing for LED colors.

Modelled after the reference: lerp the current state toward the latest
captured target with a time-constant, but snap directly when the change
is large enough that lerping would feel sluggish (e.g. a hard scene
cut).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Smoother:
    transition_seconds: float = 0.15
    """Time constant of the lerp. Smaller = snappier but more flicker.
    The reference uses 0.2 s; 0.15 gives a slightly tighter response
    that still feels smooth."""

    snap_threshold: float = 0.45
    """If any channel diff (normalised 0..1) exceeds this, abandon lerp
    and snap to target. Catches scene cuts."""

    _state: np.ndarray | None = field(default=None, init=False, repr=False)

    def step(self, target: np.ndarray, dt: float) -> np.ndarray:
        """`target` is (N, 3) uint8 from capture; returns same shape uint8."""
        t = target.astype(np.float32)
        if self._state is None:
            self._state = t.copy()
            return target

        max_diff = float(np.abs(t - self._state).max()) / 255.0
        if max_diff > self.snap_threshold:
            self._state = t
        else:
            alpha = min(1.0, dt / max(self.transition_seconds, 1e-6))
            self._state += (t - self._state) * alpha

        return np.clip(self._state, 0, 255).astype(np.uint8)
