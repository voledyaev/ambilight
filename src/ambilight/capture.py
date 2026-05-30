"""Screen capture and per-LED region averaging.

For each LED we precompute a pixel rectangle (its "look zone"). Each
frame we grab the whole display once, then numpy-slice each rectangle
and average it.

The capture itself runs through a backend so we can use the right OS
API per platform:

- macOS  → `ScreenCaptureKitBackend` (Apple's modern streaming API).
  Stable, captures hardware-decoded video, direct-display-scanout, all
  of it. Frames are streamed asynchronously into a latest-frame buffer
  and `grab()` returns the most recent one.
  Old `CGDisplayBackend` is still selectable via `--backend cgdisplay`
  for debugging but it's unstable on modern macOS — DO NOT use it.
- Windows / Linux → `MSSBackend` (mss). On Windows mss uses DXGI which
  is fast and HW-decode aware; on Linux it uses X11/PipeWire.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

from ambilight.geometry import LEFT, NUM_LEDS, RIGHT, TOP, side_for_led

RGB = tuple[int, int, int]
Rect = tuple[int, int, int, int]


@dataclass
class CaptureConfig:
    depth_fraction: float = 0.15
    monitor_index: int = 1   # used by MSS backend; CGDisplay always grabs main
    letterbox_detect: bool = True
    """Find the bounding box of non-black content each frame and anchor LED
    regions to the content's edges, not the screen's. Fixes 'strip goes
    dark' on letterboxed videos / pillarboxed YouTube."""
    letterbox_dark_threshold: int = 12
    """Pixel brightness below this is considered 'black bar' material."""
    letterbox_smoothing: float = 0.3
    """0..1 — how quickly the bbox can change between frames. 0 = instant,
    1 = never. ~0.3 keeps things stable across scene cuts without lagging."""
    skip_top_px: int = 15
    """Crop this many rows off the top of every captured frame before any
    other processing. Defaults to 15 to hide the macOS menu bar — and the
    screen-recording indicator that lives in it. Without this, the indicator's
    purple/orange dot leaks into the top-row LEDs whenever the rest of the
    screen is dark."""


# ---- backends ------------------------------------------------------


class CaptureBackend(ABC):
    screen_w: int
    screen_h: int

    @abstractmethod
    def grab(self) -> np.ndarray:
        """Return a (H, W, 4) uint8 BGRA frame of the whole display."""

    def close(self) -> None:
        pass


class MSSBackend(CaptureBackend):
    """Screen capture via mss (Windows GDI / Linux X11).

    On Windows, mss caches GDI handles (device context + bitmap) sized to
    the display state at construction time. `BitBlt` then fails — raising
    `ScreenShotError` — whenever that state shifts underneath those handles:
    a fullscreen game/video switching display mode, the screen sleeping or
    locking, a resolution/DPI change, or DRM/HDCP-protected content. Most of
    these are transient, so we recover by recreating the mss instance (which
    rebuilds the handles) and retrying. If even the retry fails, we hand back
    the last good frame rather than crashing the whole loop.
    """

    def __init__(self, monitor_index: int = 1) -> None:
        import mss  # local import so non-mss platforms don't pay the cost
        self._mss = mss
        self._monitor_index = monitor_index
        self._last: np.ndarray | None = None
        self._open()

    def _open(self) -> None:
        self._sct = self._mss.mss()
        mon = self._sct.monitors[self._monitor_index]
        self._monitor = mon
        self.screen_w = mon["width"]
        self.screen_h = mon["height"]

    def _reopen(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass
        self._open()

    def _raw_grab(self) -> np.ndarray:
        raw = self._sct.grab(self._monitor)
        return np.frombuffer(raw.raw, dtype=np.uint8).reshape(
            self.screen_h, self.screen_w, 4
        )

    def grab(self) -> np.ndarray:
        from mss.exception import ScreenShotError
        try:
            frame = self._raw_grab()
        except ScreenShotError:
            # Display state likely changed under our cached GDI handles.
            # Rebuild them and try once more.
            self._reopen()
            try:
                frame = self._raw_grab()
            except ScreenShotError:
                if self._last is not None:
                    return self._last  # protected content / mid-modeswitch
                raise
        self._last = frame
        return frame

    def close(self) -> None:
        self._sct.close()


# -- ScreenCaptureKit delegates (top-level so pyobjc can register signatures) --


if sys.platform == "darwin":
    import objc
    from CoreMedia import CMSampleBufferGetImageBuffer
    from Foundation import NSObject
    from Quartz import (  # CoreVideo bindings are shipped under Quartz
        CVPixelBufferGetBaseAddress,
        CVPixelBufferGetBytesPerRow,
        CVPixelBufferGetHeight,
        CVPixelBufferGetWidth,
        CVPixelBufferLockBaseAddress,
        CVPixelBufferUnlockBaseAddress,
        kCVPixelBufferLock_ReadOnly,
        kCVPixelFormatType_32BGRA,
    )
    from ScreenCaptureKit import (
        SCContentFilter,
        SCShareableContent,
        SCStream,
        SCStreamConfiguration,
    )
    from libdispatch import dispatch_get_global_queue

    _varlist_method_cache: dict[str, str] = {}

    def _varlist_to_array(base, size: int, h: int, bpr: int, w: int) -> np.ndarray:
        """Pull `size` bytes out of an `objc.varlist` and reshape into a
        (h, bpr // 4, 4)[:, :w, :] BGRA numpy array.

        `objc.varlist` wraps a `void *` returned from C. It exposes
        `as_buffer(count)` which returns a Python buffer-protocol object
        of `count` bytes — perfect zero-copy input for np.frombuffer.
        """
        return (
            np.frombuffer(base.as_buffer(size), dtype=np.uint8)
            .reshape(h, bpr // 4, 4)[:, :w, :]
            .copy()  # detach before the IOSurface lock releases
        )

    class _SCKFrameOutput(NSObject):  # type: ignore[no-redef]
        # Backend reference set after alloc/init (avoids initWithBackend_,
        # whose custom selector trips pyobjc signature inference).
        _backend_ref = None

        def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
            # output_type 0 = SCStreamOutputType.screen
            if output_type != 0:
                return
            try:
                pb = CMSampleBufferGetImageBuffer(sample_buffer)
                if pb is None:
                    return
                if CVPixelBufferLockBaseAddress(pb, kCVPixelBufferLock_ReadOnly) != 0:
                    return
                try:
                    w = CVPixelBufferGetWidth(pb)
                    h = CVPixelBufferGetHeight(pb)
                    bpr = CVPixelBufferGetBytesPerRow(pb)
                    base = CVPixelBufferGetBaseAddress(pb)
                    arr = _varlist_to_array(base, bpr * h, h, bpr, w)
                finally:
                    CVPixelBufferUnlockBaseAddress(pb, kCVPixelBufferLock_ReadOnly)
                if self._backend_ref is not None:
                    self._backend_ref._on_frame(arr)
            except Exception:
                import traceback
                traceback.print_exc()
                # Swallow — don't let a Python error tear down the stream.

        # Explicit ObjC signature — pyobjc otherwise infers `v@:@@@` and the
        # last arg (NSInteger SCStreamOutputType) crashes the bridge.
        stream_didOutputSampleBuffer_ofType_ = objc.selector(
            stream_didOutputSampleBuffer_ofType_,
            signature=b"v@:@@q",
        )

    class _SCKStreamDelegate(NSObject):  # type: ignore[no-redef]
        def stream_didStopWithError_(self, stream, error):
            # We don't react; SCK just complains less when delegate isn't nil.
            pass

        stream_didStopWithError_ = objc.selector(
            stream_didStopWithError_,
            signature=b"v@:@@",
        )
else:
    _SCKFrameOutput = None  # type: ignore[assignment]
    _SCKStreamDelegate = None  # type: ignore[assignment]


class ScreenCaptureKitBackend(CaptureBackend):
    """Stream-based macOS capture via Apple's ScreenCaptureKit.

    Frames arrive asynchronously on SCK's dispatch queue and are placed
    in a latest-frame buffer. `grab()` returns whatever the most recent
    one is (it may be the same as the previous call if SCK hasn't
    pushed a new frame yet — at ~60 FPS that's rare).

    Why not `CGDisplayCreateImage`? Two killer reasons on modern macOS:
    (1) it returns stale frames after the first call when polled in a
    tight loop, (2) intensive use destabilises WindowServer and locks
    the session. SCK has neither problem.

    We DO NOT set `minimumFrameInterval` or `queueDepth` — both have
    bridge bugs in pyobjc 12.x that crash the stream during start.
    """

    def __init__(self, width: int = 864, height: int = 559) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("ScreenCaptureKitBackend is macOS-only")

        self._lock = threading.Lock()
        self._latest: np.ndarray | None = None
        self._first_frame_event = threading.Event()
        self._stream = None

        # 1) Get the main display (async API)
        content_done = threading.Event()
        content_box: dict = {}

        def on_content(content, error):
            content_box["content"] = content
            content_box["error"] = error
            content_done.set()

        SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
        if not content_done.wait(timeout=10):
            raise RuntimeError("SCShareableContent fetch timed out")
        if content_box.get("error") is not None:
            raise RuntimeError(f"SCShareableContent: {content_box['error']}")
        displays = content_box["content"].displays()
        if not displays:
            raise RuntimeError("No displays found via SCShareableContent")
        display = displays[0]

        # 2) Configure stream — keep it MINIMAL.
        filt = SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        config = SCStreamConfiguration.alloc().init()
        config.setWidth_(width)
        config.setHeight_(height)
        config.setPixelFormat_(kCVPixelFormatType_32BGRA)
        config.setShowsCursor_(False)

        # 3) Wire up delegate + output handler
        self._delegate = _SCKStreamDelegate.alloc().init()
        self._handler = _SCKFrameOutput.alloc().init()
        self._handler._backend_ref = self
        self._stream = SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, config, self._delegate
        )
        queue = dispatch_get_global_queue(0, 0)
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._handler, 0, queue, None
        )
        if not ok:
            raise RuntimeError(f"SCStream.addStreamOutput failed: {err}")

        # 4) Start, wait for first frame.
        start_done = threading.Event()
        start_box: dict = {}

        def on_start(error):
            start_box["error"] = error
            start_done.set()

        self._stream.startCaptureWithCompletionHandler_(on_start)
        if not start_done.wait(timeout=10):
            raise RuntimeError("SCStream.startCapture timed out")
        if start_box.get("error") is not None:
            raise RuntimeError(f"SCStream.startCapture: {start_box['error']}")
        if not self._first_frame_event.wait(timeout=5):
            raise RuntimeError("SCStream delivered no frames within 5s")

        with self._lock:
            self.screen_h, self.screen_w = self._latest.shape[:2]

    def _on_frame(self, arr: np.ndarray) -> None:
        with self._lock:
            self._latest = arr
            self._first_frame_event.set()

    def grab(self) -> np.ndarray:
        with self._lock:
            if self._latest is None:
                raise RuntimeError("No frame from SCStream yet")
            return self._latest

    def close(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        stop_done = threading.Event()
        stream.stopCaptureWithCompletionHandler_(lambda err: stop_done.set())
        stop_done.wait(timeout=5)


class CGDisplayBackend(CaptureBackend):
    """Capture the main display via Quartz.CGDisplayCreateImage.

    Each grab is wrapped in an Objective-C autorelease pool so that the
    CGImage and CFData allocated inside are released the moment we
    return; otherwise pyobjc relies on Python GC, and on tight 60 FPS
    loops the framework holds enough framebuffer references to upset
    WindowServer and trigger a session lock when the process exits.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("CGDisplayBackend is macOS-only")
        import objc
        from Quartz import (
            CGDataProviderCopyData,
            CGDisplayCreateImage,
            CGImageGetBytesPerRow,
            CGImageGetDataProvider,
            CGImageGetHeight,
            CGImageGetWidth,
            CGMainDisplayID,
        )
        self._objc = objc
        self._CGDisplayCreateImage = CGDisplayCreateImage
        self._CGImageGetBytesPerRow = CGImageGetBytesPerRow
        self._CGImageGetDataProvider = CGImageGetDataProvider
        self._CGDataProviderCopyData = CGDataProviderCopyData

        self._display_id = CGMainDisplayID()
        with objc.autorelease_pool():
            img = self._CGDisplayCreateImage(self._display_id)
            if img is None:
                raise RuntimeError(
                    "CGDisplayCreateImage returned NULL — does the running "
                    "process have Screen Recording permission?"
                )
            self.screen_w = CGImageGetWidth(img)
            self.screen_h = CGImageGetHeight(img)
            self._stride = self._CGImageGetBytesPerRow(img) // 4

    def grab(self) -> np.ndarray:
        with self._objc.autorelease_pool():
            img = self._CGDisplayCreateImage(self._display_id)
            if img is None:
                raise RuntimeError("CGDisplayCreateImage returned NULL mid-stream")
            bpr = self._CGImageGetBytesPerRow(img)
            expected = bpr * self.screen_h
            provider = self._CGImageGetDataProvider(img)
            raw = bytes(self._CGDataProviderCopyData(provider))
            arr = (
                np.frombuffer(raw, dtype=np.uint8, count=expected)
                .reshape(self.screen_h, bpr // 4, 4)
                [:, : self.screen_w, :]
                .copy()  # detach from the about-to-be-released CFData
            )
            return arr

    def close(self) -> None:
        # Force any straggling CF refs to be freed before the process exits,
        # so WindowServer doesn't trip on lingering framebuffer references.
        import gc
        gc.collect()


def default_backend(config: CaptureConfig) -> CaptureBackend:
    if sys.platform == "darwin":
        return ScreenCaptureKitBackend()
    return MSSBackend(monitor_index=config.monitor_index)


# ---- region geometry ----------------------------------------------


def _region_for_led(
    led_index: int,
    bbox_left: int,
    bbox_top: int,
    bbox_right: int,
    bbox_bottom: int,
    depth: float,
) -> Rect:
    """Compute the LED's sample rectangle anchored to the given content
    bounding box (which may be the whole screen, or just the video area
    after letterbox stripping)."""
    side = side_for_led(led_index)
    local = led_index - side.first_led
    box_w = bbox_right - bbox_left
    box_h = bbox_bottom - bbox_top
    depth_x = max(1, int(round(box_w * depth)))
    depth_y = max(1, int(round(box_h * depth)))

    if side is RIGHT:
        slot_h = box_h / side.count
        y_top = int(round(bbox_bottom - (local + 1) * slot_h))
        y_bot = int(round(bbox_bottom - local * slot_h))
        return (bbox_right - depth_x, max(0, y_top), bbox_right, y_bot)

    if side is TOP:
        slot_w = box_w / side.count
        x_left = int(round(bbox_right - (local + 1) * slot_w))
        x_right = int(round(bbox_right - local * slot_w))
        return (max(0, x_left), bbox_top, x_right, bbox_top + depth_y)

    if side is LEFT:
        slot_h = box_h / side.count
        y_top = int(round(bbox_top + local * slot_h))
        y_bot = int(round(bbox_top + (local + 1) * slot_h))
        return (bbox_left, max(0, y_top), bbox_left + depth_x, y_bot)

    raise AssertionError(f"unhandled side {side.name}")


def _detect_letterbox_bbox(
    small_bgra: np.ndarray, dark_threshold: int
) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) of the brightest content area in
    `small_bgra` (a downsampled BGRA image), or `None` if the whole image
    is below threshold.

    A row/column is considered a "black bar" if NONE of its pixels exceed
    `dark_threshold` in the max colour channel. We crop until we hit the
    first row/column that has at least one bright pixel.
    """
    # Per-pixel max channel (rough luminance proxy that ignores hue).
    # small_bgra is (H, W, 4); take BGR channels only.
    luma = small_bgra[:, :, :3].max(axis=2)
    row_bright = luma.max(axis=1) > dark_threshold
    col_bright = luma.max(axis=0) > dark_threshold
    if not row_bright.any() or not col_bright.any():
        return None
    rows = np.where(row_bright)[0]
    cols = np.where(col_bright)[0]
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


# ---- public capture loop ------------------------------------------


# Backends that hand us Retina-sized frames (CGDisplay at 3456×2234) benefit
# enormously from a stride downsample before per-region averaging. SCK
# already gives us a small frame so further downsampling is a no-op there.
def _stride_for(screen_w: int) -> int:
    return 8 if screen_w >= 1500 else 1


@dataclass
class ScreenCapture:
    config: CaptureConfig = field(default_factory=CaptureConfig)
    backend: CaptureBackend | None = None

    _bbox_small: tuple[float, float, float, float] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = default_backend(self.config)
        self._setup_geometry()

    def _setup_geometry(self) -> None:
        """(Re)compute per-LED sample geometry from the backend's current
        frame size. Called once at init, and again whenever the backend's
        reported resolution changes mid-run (e.g. mss rebuilt its handles
        after a display mode switch), so the regions don't stay anchored to
        a stale resolution."""
        self._backend_w = self.backend.screen_w
        self._backend_h = self.backend.screen_h
        self.screen_w = self.backend.screen_w
        # Drop the top N rows up-front so downstream geometry stays
        # consistent. With SCK on macOS this hides the menu bar (and
        # its always-on screen-recording indicator) from every LED.
        self._top_crop = max(0, self.config.skip_top_px)
        self.screen_h = max(1, self.backend.screen_h - self._top_crop)
        self._stride = _stride_for(self.screen_w)
        self._bbox_small = None  # stale in the new resolution's coords
        self.regions: list[Rect] = [
            _region_for_led(
                i, 0, 0, self.screen_w, self.screen_h, self.config.depth_fraction
            )
            for i in range(NUM_LEDS)
        ]

    def grab_colors(self) -> np.ndarray:
        full = self.backend.grab()  # (H, W, 4) BGRA
        if (self.backend.screen_w != self._backend_w
                or self.backend.screen_h != self._backend_h):
            self._setup_geometry()
        if self._top_crop > 0:
            full = full[self._top_crop :, :, :]
        s = self._stride
        if s == 1:
            small = full  # already small (e.g. SCK-supplied frame)
        else:
            small = np.ascontiguousarray(full[::s, ::s])
        small_h, small_w = small.shape[:2]

        # Find the content bbox in the downsampled frame, then EMA-smooth
        # it so dark scenes / quick cuts don't make the regions jitter.
        if self.config.letterbox_detect:
            detected = _detect_letterbox_bbox(
                small, self.config.letterbox_dark_threshold
            )
        else:
            detected = None
        if detected is None:
            detected = (0, 0, small_w, small_h)

        if self._bbox_small is None:
            self._bbox_small = tuple(float(x) for x in detected)  # type: ignore[assignment]
        else:
            a = self.config.letterbox_smoothing
            self._bbox_small = tuple(  # type: ignore[assignment]
                a * old + (1 - a) * new
                for old, new in zip(self._bbox_small, detected)
            )

        # Convert smoothed bbox back to full-resolution coords for region
        # calculation, then map regions into small-image coords for sampling.
        bl, bt, br, bb = (int(round(v)) for v in self._bbox_small)
        regions_small = [
            _region_for_led(
                i, bl, bt, br, bb, self.config.depth_fraction
            )
            for i in range(NUM_LEDS)
        ]

        result = np.empty((NUM_LEDS, 3), dtype=np.uint8)
        for i, (l, t, r, b) in enumerate(regions_small):
            # Clamp to image bounds
            l = max(0, min(l, small_w))
            r = max(l + 1, min(r, small_w))
            t = max(0, min(t, small_h))
            b = max(t + 1, min(b, small_h))
            patch = small[t:b, l:r]
            if patch.size == 0:
                result[i] = 0
                continue
            mean = patch.mean(axis=(0, 1))
            result[i, 0] = mean[2]  # R
            result[i, 1] = mean[1]  # G
            result[i, 2] = mean[0]  # B
        return result

    def close(self) -> None:
        self.backend.close()
