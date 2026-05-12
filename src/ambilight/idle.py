"""Decide when the LED strip should go dark.

The OS dims the display after a period of inactivity but leaves the
framebuffer painted — screen capture keeps reading bright pixels, so
without intervention the strip stays lit. We expose
:func:`should_strip_be_off` which combines whichever signals each
platform exposes:

- **Windows**: subscribe to ``GUID_CONSOLE_DISPLAY_STATE`` and follow
  the actual monitor backlight state (on / dim / off). The strip
  mirrors the panel directly — fullscreen video keeps the monitor on
  (player holds ``ES_DISPLAY_REQUIRED``), so the strip stays on too;
  the moment the OS dims the panel, the strip follows.
- **macOS**: query ``pmset -g assertions`` for
  ``PreventUserIdleDisplaySleep`` and combine with the input-idle
  timer. If any app holds a display-wake assertion (video / movie
  apps do) we never turn off; otherwise we honour the user-supplied
  idle timeout.
- **Linux**: unsupported; returns ``None`` and the caller falls back
  to leaving the strip on.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Callable


# ---- input idle (used by macOS branch) ---------------------------------


def _darwin_input_idle() -> Callable[[], float]:
    from Quartz import CGEventSourceSecondsSinceLastEventType

    def fn() -> float:
        return float(CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF))

    return fn


_get_idle_seconds: Callable[[], float] | None = None
if sys.platform == "darwin":
    try:
        _get_idle_seconds = _darwin_input_idle()
    except Exception:
        _get_idle_seconds = None


# ---- macOS: pmset-based display-assertion check ------------------------


def _macos_display_assertion_held() -> bool:
    """True iff any process holds ``PreventUserIdleDisplaySleep``.

    Apps that play video (browsers, VLC, QuickTime) hold this
    assertion to stop the panel from dimming during playback. We
    treat it as a definitive 'do not turn the strip off' signal.
    """
    try:
        out = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True, text=True, timeout=1.0,
        )
    except Exception:
        return False
    for line in out.stdout.splitlines():
        # Header lines look like:  "   PreventUserIdleDisplaySleep    1"
        parts = line.split()
        if len(parts) == 2 and parts[0] == "PreventUserIdleDisplaySleep":
            try:
                return int(parts[1]) > 0
            except ValueError:
                return False
    return False


# ---- Windows: GUID_CONSOLE_DISPLAY_STATE notification ------------------


_win32_display_on: bool | None = None
_win32_started = False
_win32_lock = threading.Lock()


def _start_win32_display_watcher() -> None:
    """Spawn a daemon thread that hosts a message-only window and
    listens for ``GUID_CONSOLE_DISPLAY_STATE`` power-setting events.

    Updates the module-level ``_win32_display_on`` whenever Windows
    notifies us that the panel went on / dim / off. Initial state is
    ``True`` (assume the monitor is on) — Windows only fires the
    notification on transitions, so we have nothing to query for the
    current state on startup.
    """
    global _win32_started, _win32_display_on
    if _win32_started:
        return
    _win32_started = True
    _win32_display_on = True

    def pump() -> None:
        import ctypes
        from ctypes import wintypes

        global _win32_display_on

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        LRESULT = ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class POWERBROADCAST_SETTING(ctypes.Structure):
            _fields_ = [
                ("PowerSetting", ctypes.c_byte * 16),
                ("DataLength", wintypes.DWORD),
                ("Data", ctypes.c_byte * 4),
            ]

        # GUID_CONSOLE_DISPLAY_STATE = {6FBD9CC2-6D4F-11DA-8BDE-E01EA3D8C65F}
        GUID_BYTES = (ctypes.c_byte * 16)(
            0xC2, 0x9C, 0xBD, 0x6F,  # Data1 little-endian
            0x4F, 0x6D,              # Data2 little-endian
            0xDA, 0x11,              # Data3 little-endian
            0x8B, 0xDE, 0xE0, 0x1E, 0xA3, 0xD8, 0xC6, 0x5F,  # Data4 raw
        )

        HWND_MESSAGE = wintypes.HWND(-3)
        WM_POWERBROADCAST = 0x0218
        PBT_POWERSETTINGCHANGE = 0x8013
        DEVICE_NOTIFY_WINDOW_HANDLE = 0x0

        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        user32.RegisterPowerSettingNotification.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ]

        def wnd_proc(hwnd, msg, wparam, lparam):
            global _win32_display_on
            if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE:
                try:
                    pbs = ctypes.cast(
                        lparam, ctypes.POINTER(POWERBROADCAST_SETTING),
                    ).contents
                    # Data[0]: 0=off, 1=on, 2=dimmed
                    with _win32_lock:
                        _win32_display_on = (pbs.Data[0] == 1)
                except Exception:
                    pass
                return 1
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc_c = WNDPROC(wnd_proc)

        wc = WNDCLASSW()
        wc.lpfnWndProc = wnd_proc_c
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "AmbilightDisplayState"
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return

        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, None, 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None,
        )
        if not hwnd:
            return

        if not user32.RegisterPowerSettingNotification(
            hwnd, ctypes.byref(GUID_BYTES), DEVICE_NOTIFY_WINDOW_HANDLE,
        ):
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessageW(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    t = threading.Thread(target=pump, daemon=True, name="ambilight-display-watch")
    t.start()


# ---- public API --------------------------------------------------------


def should_strip_be_off(idle_timeout_s: float) -> bool | None:
    """Return True if the LED strip should currently be dark.

    ``idle_timeout_s`` is only consulted on macOS, where we don't have
    a direct 'display went dim' signal and fall back to input-idle as
    a proxy. On Windows the value is ignored — we sync to the actual
    panel state. ``None`` means the answer can't be determined on this
    platform; caller should default to leaving the strip on.
    """
    if sys.platform == "darwin":
        if _get_idle_seconds is None:
            return None
        if _macos_display_assertion_held():
            return False
        return _get_idle_seconds() >= idle_timeout_s

    if sys.platform == "win32":
        _start_win32_display_watcher()
        with _win32_lock:
            on = _win32_display_on
        if on is None:
            return None
        return not on

    return None


def supported() -> bool:
    return sys.platform in ("darwin", "win32")
