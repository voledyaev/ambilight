"""Ambilight main loop."""

from __future__ import annotations

import argparse
import sys
import time


def _hide_macos_dock_icon() -> None:
    """Stop the Python launcher from appearing in the Dock.

    pyobjc auto-registers the process as a GUI app the moment we touch
    AppKit/ScreenCaptureKit, which lands a bouncing python rocket in
    the Dock. ``NSApplicationActivationPolicyProhibited`` (= 2) drops
    us back to a pure background daemon — no icon, no Cmd-Tab entry.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(2)
    except Exception:
        pass


_hide_macos_dock_icon()

import numpy as np

from ambilight.capture import (
    CaptureConfig,
    CGDisplayBackend,
    MSSBackend,
    ScreenCapture,
    ScreenCaptureKitBackend,
)
from ambilight.device import AmbilightDevice
from ambilight.geometry import NUM_LEDS
from ambilight.idle import should_strip_be_off, supported as idle_supported
from ambilight.smooth import Smoother


def _make_backend(name: str, config: CaptureConfig):
    if name == "auto":
        return None  # default_backend() picks SCK on macOS, MSS elsewhere
    if name == "sck":
        return ScreenCaptureKitBackend()
    if name == "cgdisplay":
        return CGDisplayBackend()
    if name == "mss":
        return MSSBackend(monitor_index=config.monitor_index)
    raise SystemExit(f"unknown backend {name!r}")


def run(args: argparse.Namespace) -> int:
    config = CaptureConfig(
        depth_fraction=args.depth,
        monitor_index=args.monitor,
        skip_top_px=args.skip_top,
    )
    capture = ScreenCapture(config=config, backend=_make_backend(args.backend, config))
    smoother = Smoother(
        transition_seconds=args.smooth,
        snap_threshold=args.snap,
    )
    target_dt = 1.0 / args.fps

    idle_timeout = args.idle_timeout
    idle_enabled = idle_timeout > 0 and idle_supported()
    if idle_timeout > 0 and not idle_supported():
        print("  --idle-timeout requested but no idle provider on this platform; "
              "feature disabled.")

    print(
        f"Backend: {type(capture.backend).__name__}\n"
        f"Display: {capture.screen_w}×{capture.screen_h} "
        f"(monitor #{args.monitor})\n"
        f"Depth: {args.depth * 100:.0f}%, "
        f"smoothing: {args.smooth:.2f} s, "
        f"snap @ {args.snap * 100:.0f}%, "
        f"target {args.fps:.0f} FPS"
        + (f"\nIdle off: {idle_timeout:.0f}s (macOS) / display-state (Windows)"
           if idle_enabled else "")
    )

    with AmbilightDevice(
        rle_tolerance=args.rle_tolerance,
        dedup_tolerance=args.dedup_tolerance,
    ) as dev:
        last = time.monotonic()
        report_at = last
        frames_in_window = 0
        cap_ms = 0.0
        send_ms = 0.0
        is_idle_off = False
        try:
            while True:
                t0 = time.monotonic()
                dt = t0 - last
                last = t0

                if idle_enabled:
                    if should_strip_be_off(idle_timeout):
                        if not is_idle_off:
                            dev.send_solid((0, 0, 0))
                            is_idle_off = True
                            print("  display idle — strip off", flush=True)
                        time.sleep(1.0)
                        last = time.monotonic()
                        continue
                    if is_idle_off:
                        is_idle_off = False
                        print("  display active — strip back on", flush=True)

                target = capture.grab_colors()
                t1 = time.monotonic()
                smoothed = smoother.step(target, dt)
                colors = [(int(r), int(g), int(b)) for r, g, b in smoothed]
                dev.send_leds(colors)
                t2 = time.monotonic()

                cap_ms += (t1 - t0) * 1000
                send_ms += (t2 - t1) * 1000
                frames_in_window += 1

                if t2 - report_at >= 2.0:
                    fps = frames_in_window / (t2 - report_at)
                    sent = dev.frames_sent
                    skipped = dev.frames_skipped
                    dev.frames_sent = 0
                    dev.frames_skipped = 0
                    print(
                        f"  {fps:5.1f} FPS  "
                        f"capture {cap_ms / frames_in_window:5.1f} ms  "
                        f"send {send_ms / frames_in_window:5.1f} ms  "
                        f"USB: {sent} sent / {skipped} skipped",
                        flush=True,
                    )
                    frames_in_window = 0
                    cap_ms = send_ms = 0.0
                    report_at = t2

                remaining = target_dt - (time.monotonic() - t0)
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            print("\nStopped by user.")
    capture.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ambilight")
    p.add_argument(
        "--depth", type=float, default=0.30,
        help="How far each LED looks into the screen, as fraction 0..1 (default 0.30). "
             "Bigger values let LEDs see past letterbox black bars at the cost of less "
             "spatial precision near the edges.",
    )
    p.add_argument(
        "--smooth", type=float, default=0.15,
        help="Software smoothing time constant in seconds (default 0.15). 0 disables it.",
    )
    p.add_argument(
        "--snap", type=float, default=0.45,
        help="Snap-to-target threshold; if any channel changes by more than "
             "this fraction of full range, ignore smoothing for that frame "
             "(default 0.45 — handles scene cuts).",
    )
    p.add_argument(
        "--fps", type=float, default=15.0,
        help="Target FPS (default 15). Each frame splits into ~3 chunks with 20 ms "
             "gaps over USB, so the effective hardware ceiling is ~15-16 FPS for "
             "this strip's 65 LEDs.",
    )
    p.add_argument(
        "--monitor", type=int, default=1,
        help="mss monitor index: 1 = primary (default), 2+ = secondaries. "
             "Ignored by the cgdisplay backend (always grabs the main display).",
    )
    p.add_argument(
        "--backend", choices=("auto", "sck", "cgdisplay", "mss"), default="auto",
        help="Screen capture backend. 'auto' picks SCK on macOS / MSS elsewhere. "
             "'cgdisplay' is the old broken path — known to lock sessions, "
             "do not use except for direct comparison.",
    )
    p.add_argument(
        "--skip-top", type=int, default=15,
        help="Crop this many top rows from the captured frame before processing. "
             "Default 15 hides the macOS menu bar (and its screen-recording "
             "indicator) from the top-row LEDs. Set to 0 to disable.",
    )
    p.add_argument(
        "--rle-tolerance", type=int, default=4,
        help="Adjacent LEDs whose colour differs by ≤ this per channel are merged "
             "into one wire entry (default 4). 0 = lossless, no merging. Higher "
             "values pack more LEDs into fewer entries → fewer USB chunks per "
             "frame → higher effective FPS, at the cost of slightly less colour "
             "gradient fidelity.",
    )
    p.add_argument(
        "--idle-timeout", type=float, default=120.0,
        help="Turn the strip off after this many seconds with no keyboard / "
             "mouse input (default 120). The OS dims the display on idle but "
             "doesn't blank the framebuffer, so without this the strip stays "
             "lit. Set to 0 to disable. Linux: unsupported, flag is a no-op.",
    )
    p.add_argument(
        "--dedup-tolerance", type=int, default=1,
        help="If every LED stayed within ±N of its previous value, skip the USB "
             "write entirely (default 1). Saves bandwidth and MCU work on static "
             "screens. Set to 0 to disable.",
    )
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
