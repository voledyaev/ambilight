# ambilight

DIY screen-sync software for the no-name **ROBOBLOQ QuikLight** USB LED
strip (VID/PID `1A86:FE07`) sold under various Chinese-OEM brands. The
shipped "SyncLight" app produces 5+ seconds of colour-change lag and
splits the strip into 12 coarse zones with no per-side geometry. This
client fixes both and runs in the background with no Dock icon.

What you get versus OEM:

| | OEM SyncLight | This client |
|---|---|---|
| Latency | 5+ seconds | one frame (~60 ms) |
| Per-side layout | Linear ranges, no geometry | 17/31/17 LED zones matching the actual strip |
| Letterbox handling | None — strip dims with bars | Bbox detection, follows video content |
| macOS fullscreen video | Captures placeholder | True frame via ScreenCaptureKit |
| WindowServer stability | Fine | Fine (after we migrated away from CGDisplay) |
| Dock icon | Bouncing python rocket | Hidden — pure background daemon |

## Quickstart

```bash
git clone git@github.com:voledyaev/ambilight.git
cd ambilight
uv sync
uv run ambilight
```

On macOS, grant **Screen Recording** permission when prompted (the first
run will trigger the system dialog). Restart the command after granting.

The strip immediately starts following your screen. `Ctrl+C` to stop.

## CLI options

```
uv run ambilight [flags]

--fps N              Target FPS (default 15). Hardware ceiling is ~30 FPS
                     with the default protocol settings; RLE often gets you
                     30+ on calm content for free.

--depth F            Fraction of the screen each LED looks at, 0..1
                     (default 0.30). Increase past common letterbox sizes
                     for video content (0.20 is the smallest sensible value).

--smooth S           Software EMA smoothing time constant in seconds
                     (default 0.15). 0 disables it.

--snap S             Snap-to-target threshold; channel changes ≥ S × 255
                     bypass smoothing for that frame so scene cuts hit
                     instantly (default 0.45).

--rle-tolerance N    Adjacent LEDs within ±N per channel get merged into
                     one wire entry (default 4). Higher → fewer USB chunks,
                     higher effective FPS, less gradient fidelity.

--dedup-tolerance N  Skip the USB write entirely if every LED stayed within
                     ±N of its previous value (default 1). 0 disables it.

--skip-top N         Crop N rows off the top of the captured frame before
                     processing (default 15 — hides the macOS menu bar and
                     its screen-recording indicator).

--backend B          auto | sck | cgdisplay | mss. Default 'auto' picks
                     SCK on macOS and MSS elsewhere. 'cgdisplay' is the
                     old broken path — do not use it.

--monitor N          mss monitor index, 1 = primary (ignored by SCK).
```

Status line during run:

```
Backend: ScreenCaptureKitBackend
Display: 864×544 (monitor #1)
Depth: 30%, smoothing: 0.15 s, snap @ 45%, target 15 FPS
  20.6 FPS  capture  9.1 ms  send  4.2 ms  USB: 23 sent / 18 skipped
```

`USB: N sent / M skipped` is the dedup counter — static screens push the
skip count way up and let the strip rest.

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                          main loop                          │
└──────┬──────────────────────────┬──────────────────────────┘
       │                          │
       ▼                          ▼
┌──────────────┐         ┌─────────────────┐
│   capture    │         │     device      │
│   ────────   │         │   ──────────    │
│  SCK / mss   │         │  hidapi → strip │
│  backend     │         │                 │
│  letterbox   │         │  RLE + dedup +  │
│  detect      │         │  chunked HID    │
└──────┬───────┘         └────────▲────────┘
       │                          │
       │      ┌──────────┐       │
       └─────▶│  smooth  │───────┘
              │  ──────  │
              │ EMA +    │
              │ snap     │
              └──────────┘
```

Modules:

- `ambilight.capture` — pluggable screen-capture backends with letterbox
  detection. macOS uses **ScreenCaptureKit** via pyobjc (stable, sees
  hardware-decoded video, no WindowServer stalls). Windows/Linux fall
  back to `mss` automatically. Per-LED averaging happens on a
  downsampled frame for speed.
- `ambilight.geometry` — the strip's physical layout: 17 LEDs right,
  31 top, 17 left, starting from bottom-right corner CCW.
- `ambilight.smooth` — exponential moving average with a snap-threshold
  to keep scene cuts crisp.
- `ambilight.protocol` — wire format builder. `colors_to_entries`
  produces 5-byte range entries `(start, R, G, B, end)` with optional
  run-length merging.
- `ambilight.device` — HID transport with frame deduplication. Splits
  oversize frames into multiple 64-byte HID reports with a 20 ms gap
  (matching OEM cadence).
- `ambilight.cli` — argparse main loop. Hides the Python Dock icon via
  `NSApplicationActivationPolicyProhibited` so it runs as a pure
  background utility.

## Hardware notes

The strip's controller appears as a vendor-defined HID device on
USB Full-Speed (12 Mbps). Inspect it with:

```bash
uv run python scripts/probe_descriptor.py
```

Expected output:

```
Device 0x1a86:0xfe07
  Speed: FULL (12 Mbps)
Configuration 1:
  Total interfaces: 2
  Interface 0 (alt 0):                      ← this is our target
    Class/SubClass/Proto: 3/0/0
    Endpoints: 2
      EP 0x01 (OUT, INTERRUPT): max=64 B, bInterval=1 (~1 ms poll)
      EP 0x82 (IN,  INTERRUPT): max=64 B, bInterval=1 (~1 ms poll)
  Interface 1 (alt 0):                      ← fake boot keyboard, ignored
    Class/SubClass/Proto: 3/1/1
```

The strip has 65 LEDs laid out as 17 (right edge, bottom to top) + 31
(top edge, right to left) + 17 (left edge, top to bottom). Protocol
addressing is 1-based: address `N` paints physical LED `N-1`.

## Reverse-engineering — things that bit us

A summary of the dead ends we hit so future-you (or you) doesn't repeat
them.

### "Firmware has a built-in 5-second fade" — wrong

For a long time we thought the strip's MCU did an inherent slow fade
between any two colours. Replicating the protocol from
[xsiravia/win_ambilight](https://github.com/xsiravia/win_ambilight)
(designed for a 36-LED variant) gave us partial behaviour with that
apparent fade. We sent frames at all the FPS rates in the book; nothing
made it faster.

**It wasn't a fade.** It was the firmware silently dropping malformed
frames at a high rate, and the slow visual update was just the interval
between *accepted* frames. The single byte that was wrong is below.

### The 5-byte entry is a *range*, not a paired address

The xsiravia/win_ambilight reference treats the 5-byte LED entry as
`(addr1, addr2, R, G, B)` — two paired LED addresses and the colour
they share. We blindly copied this shape.

The real format, lifted directly out of the OEM SyncLight 2.18.1
Electron bundle:

```javascript
s.push(h);      // start_address
s.push(g[0]);   // R
s.push(g[1]);   // G
s.push(g[2]);   // B
s.push(f);      // end_address
```

It's `(start_addr, R, G, B, end_addr)` — a *range* of LEDs that share
one colour. The 5-second "fade" we observed went away the instant we
fixed this. Bonus: ranges enable run-length encoding for free, which is
how we beat OEM's effective FPS too.

### Addresses are 1-based, and you need the last one

Address `N` paints physical LED `N-1`. For a 65-LED strip the valid
range is `1..65` — **not** `1..64`. We wasted an evening on why the
last LED was always stuck on whatever colour it had last received.

### CGDisplay is broken on modern macOS

We started with `mss` (uses `CGWindowListCreateImage`) which can't see
hardware-decoded video. Switched to `CGDisplayCreateImage`, which does
see HW video but returns *stale* frames after the first few calls,
intermittently crashes WindowServer (kicked us out to lock screen), and
on Apple Silicon caps at about 12 FPS due to CPU-readback of the full
Retina framebuffer. **Use ScreenCaptureKit.** It's the only macOS API
Apple still actually supports for streaming capture, and pyobjc bindings
work fine once you give the delegate methods explicit ObjC type
signatures (`v@:@@q` for `stream:didOutputSampleBuffer:ofType:`).

### `objc.varlist` instead of a `void*`

`CVPixelBufferGetBaseAddress` returns an `objc.varlist`, not a Python
int. `ctypes.from_address(varlist)` blows up. The right path is
`varlist.as_buffer(byte_count)` which gives you something
`np.frombuffer` accepts directly — zero-copy, fast.

### The OEM software has the same 5-second lag

If you run "SyncLight" in *Movie mode* (default) you'll see the slow
fade we initially thought was a firmware limitation. But *Game mode* in
the same OEM app is fast — and the only difference in the source is
that it skips a software-side smoother in the Electron app:

```javascript
1 === syncMode && (X = k(e, L, K), X = w(V, G, X));
```

Same wire protocol either way. We don't apply that smoother (our own
`ambilight.smooth` is much finer-grained), so we get game-mode speed
plus our better geometry.

### The Python Dock icon bouncing

The moment you touch any pyobjc framework that pulls in AppKit (which
ScreenCaptureKit does), macOS registers the process as a GUI app and
parks an icon in the Dock. The icon bounces because *something* in the
system thinks our process wants foreground attention — possibly the
screen-recording session itself.

Fix: at startup, before any AppKit-touching code,

```python
from AppKit import NSApplication
NSApplication.sharedApplication().setActivationPolicy_(2)  # Prohibited
```

`2 = NSApplicationActivationPolicyProhibited` — no Dock icon, no
Cmd-Tab entry, true background daemon.

## Scripts

`scripts/` holds the diagnostic tools we kept around after the
reverse-engineering campaign:

- `test_static.py` — paint the strip a solid colour; quick smoke test
  that USB + protocol work.
- `smoke_open.py` — open the HID device only, dump enumerated info.
- `probe_descriptor.py` — print USB descriptors (VID/PID, endpoints,
  bInterval).
- `probe_live_capture.py` — capture a few frames, save them with the
  LED region overlays drawn on top. Useful for debugging when colours
  don't match what you expect.
- `discover_per_led.py` — paint each LED a unique pattern (white head,
  RGB gradients per side) so you can confirm/discover the strip's
  geometry if you ever connect a different layout.

## Status

Targeted at one specific 65-LED strip variant on macOS Sequoia (Darwin
25.x) on Apple Silicon. The capture pipeline has both `mss` (Windows /
Linux) and `cgdisplay` (broken-but-kept-for-debug) backends, but
they're not actively tested. PRs welcome.
