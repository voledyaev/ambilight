"""USB transport for the ROBOBLOQ QuikLight strip via hidapi.

Uses the OEM setSectionLED protocol with chunked HID writes. See
:mod:`ambilight.protocol` for the wire format details.
"""

from __future__ import annotations

import time

import hid

from ambilight.geometry import NUM_LEDS
from ambilight.protocol import CHUNK_GAP_S, build_section_chunks

VENDOR_ID = 0x1A86
PRODUCT_ID = 0xFE07
TARGET_INTERFACE = 0  # interface 1 is a fake boot keyboard, we ignore it


class AmbilightDeviceError(RuntimeError):
    pass


class AmbilightDevice:
    """hidapi-backed connection to the strip's vendor HID interface."""

    def __init__(self, rle_tolerance: int = 4, dedup_tolerance: int = 1) -> None:
        self._dev = hid.device()
        self._set_id = 1
        self._opened = False
        self._rle_tolerance = rle_tolerance
        self._dedup_tolerance = dedup_tolerance
        self._last_colors: list | None = None
        self.frames_sent = 0
        self.frames_skipped = 0

    def open(self) -> None:
        path = self._find_interface_path()
        self._dev.open_path(path)
        self._dev.set_nonblocking(False)
        self._opened = True

    def close(self) -> None:
        if self._opened:
            self._dev.close()
            self._opened = False

    def _reopen(self) -> None:
        """Tear down and re-establish the HID connection.

        Used to recover from a transient USB failure (device slept,
        re-enumerated, momentary bus hiccup) without killing the loop.
        """
        try:
            self._dev.close()
        except Exception:
            pass
        self._opened = False
        self._dev = hid.device()
        self.open()

    def __enter__(self) -> "AmbilightDevice":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _find_interface_path(self) -> bytes:
        candidates = [
            d for d in hid.enumerate(VENDOR_ID, PRODUCT_ID)
            if d.get("interface_number", -1) == TARGET_INTERFACE
        ]
        if not candidates:
            seen = [d.get("interface_number") for d in hid.enumerate(VENDOR_ID, PRODUCT_ID)]
            raise AmbilightDeviceError(
                f"device {VENDOR_ID:#06x}:{PRODUCT_ID:#06x} interface "
                f"{TARGET_INTERFACE} not found (saw: {seen})"
            )
        return candidates[0]["path"]

    def send_leds(self, colors) -> None:
        """Paint the whole strip. `colors` is a sequence of (R,G,B) per
        physical LED in order, length should match the strip's LED count
        (65 for this hardware).

        Skips the USB write entirely if every channel of every LED is
        within ``dedup_tolerance`` of the previous frame's value (so a
        static screen doesn't keep hammering the bus).
        """
        if not self._opened:
            raise AmbilightDeviceError("device is not open")
        if hasattr(colors, "tolist"):
            colors = [tuple(int(c) for c in row) for row in colors]

        if self._last_colors is not None and self._is_within_tolerance(colors):
            self.frames_skipped += 1
            return

        chunks, next_set_id = build_section_chunks(
            colors, self._set_id, rle_tolerance=self._rle_tolerance,
        )
        try:
            self._write_chunks(chunks)
        except (OSError, ValueError, AmbilightDeviceError) as exc:
            # Transient USB failure (device slept / re-enumerated / bus
            # hiccup). Reconnect once and retry the same frame; only give
            # up if the second attempt also fails.
            try:
                self._reopen()
            except Exception as reopen_exc:
                raise AmbilightDeviceError(
                    f"USB write failed and reconnect failed: {reopen_exc}"
                ) from exc
            self._write_chunks(chunks)

        # Only commit state after the frame is actually on the wire, so a
        # failed/partial send doesn't make the next frame wrongly dedup-skip.
        self._set_id = next_set_id
        self._last_colors = list(colors)
        self.frames_sent += 1

    def _write_chunks(self, chunks) -> None:
        for i, pkt in enumerate(chunks):
            written = self._dev.write(b"\x00" + pkt)
            if written < 0:
                raise AmbilightDeviceError(
                    f"hid_write returned {written}: {self._dev.error()!r}"
                )
            if i < len(chunks) - 1:
                time.sleep(CHUNK_GAP_S)

    def _is_within_tolerance(self, colors) -> bool:
        tol = self._dedup_tolerance
        last = self._last_colors
        if last is None or len(last) != len(colors):
            return False
        for cur, prev in zip(colors, last):
            if (abs(cur[0] - prev[0]) > tol
                    or abs(cur[1] - prev[1]) > tol
                    or abs(cur[2] - prev[2]) > tol):
                return False
        return True

    def send_solid(self, color, n_leds: int = NUM_LEDS) -> None:
        """Convenience: paint every LED a single colour."""
        self.send_leds([color] * n_leds)
