"""Wire format for the ROBOBLOQ QuikLight USB LED strip.

Reverse-engineered the hard way from SyncLight 2.18.1 (Robobloq's OEM
Electron app). Two key insights it took us a while to get to:

1. The 5-byte entry is **NOT** ``(addr1, addr2, R, G, B)``. It's
   ``(start_addr, R, G, B, end_addr)`` — a *range* of LEDs that share
   one colour. This is also what makes the OEM software's per-pixel
   colouring "linear" — they emit only ~12 large ranges per frame.
   Per-LED granularity is ours to use because *we* build smaller ranges.

2. Addresses are 1-based: protocol address ``N`` paints physical LED
   ``N-1``. For our 65-LED strip the valid addresses are 1..65.

We send via ``setSectionLED`` (action 134, ``"RB"`` header, single-byte
length). Each USB packet holds up to 11 entries (= 55 bytes payload, 61
bytes total — fits in a single 64-byte HID report). Whole-strip
updates therefore split into a small handful of chunks with a 20 ms
gap between them, matching what OEM does.

Packet layout::

    offset  byte
    ------  ----
    0-1     'R' 'B'                (0x52 0x42)
    2       total length (uint8)
    3       setID                  (transaction id, 1..254)
    4       134                    (action = setSectionLED)
    5..N-2  N entries × 5 bytes    (start, R, G, B, end)
    N-1     checksum               (sum of all prior bytes mod 256)
"""

from __future__ import annotations

ACTION_SECTION_LED = 134
ENTRIES_PER_CHUNK = 11  # 11 × 5 = 55 byte payload, 61 byte packet — fits in one HID report
CHUNK_GAP_S = 0.020      # 20 ms between consecutive chunks of one frame, per OEM

RGB = tuple[int, int, int]
Entry = tuple[int, int, int, int, int]  # start, R, G, B, end


def _checksum(buf: bytes) -> int:
    return sum(buf) % 256


def _clip(v: int) -> int:
    return 0 if v < 0 else (255 if v > 255 else v)


def colors_to_entries(colors: list[RGB], rle_tolerance: int = 4) -> list[Entry]:
    """Compress per-LED colours into range entries.

    Each entry is ``(start_addr, R, G, B, end_addr)`` and paints LEDs
    ``start_addr..end_addr`` (inclusive, 1-based) a single colour. Runs
    of consecutive LEDs whose colour stays within ±``rle_tolerance`` per
    channel are merged into one entry — for a typical ambilight scene
    with 3-5 colour zones this drops the entry count from 33 (worst-case
    paired) to ~5, which often fits in a single 11-entry HID chunk.
    Result: 1 chunk per frame instead of 3, no inter-chunk sleeps, much
    higher effective FPS on calm content.

    The emitted colour for each run is the integer average of all LEDs
    in the run so the merge stays faithful to what was on screen.
    """
    n = len(colors)
    if n == 0:
        return []

    entries: list[Entry] = []
    run_start = 0
    run_sum_r = colors[0][0]
    run_sum_g = colors[0][1]
    run_sum_b = colors[0][2]
    run_len = 1
    ref = colors[0]

    def emit(start_idx: int, length: int, sr: int, sg: int, sb: int) -> None:
        r = _clip(sr // length)
        g = _clip(sg // length)
        b = _clip(sb // length)
        entries.append((start_idx + 1, r, g, b, start_idx + length))

    for i in range(1, n):
        c = colors[i]
        if (abs(c[0] - ref[0]) <= rle_tolerance
                and abs(c[1] - ref[1]) <= rle_tolerance
                and abs(c[2] - ref[2]) <= rle_tolerance):
            run_sum_r += c[0]
            run_sum_g += c[1]
            run_sum_b += c[2]
            run_len += 1
        else:
            emit(run_start, run_len, run_sum_r, run_sum_g, run_sum_b)
            run_start = i
            run_sum_r, run_sum_g, run_sum_b = c[0], c[1], c[2]
            run_len = 1
            ref = c

    emit(run_start, run_len, run_sum_r, run_sum_g, run_sum_b)
    return entries


def build_section_chunks(
    colors: list[RGB],
    set_id_start: int = 1,
    rle_tolerance: int = 4,
) -> tuple[list[bytes], int]:
    """Build the sequence of HID payloads that paint the whole strip.

    Returns ``(packets, next_set_id)`` so the caller can keep setID
    monotonically increasing across frames (OEM does, so we do too).
    """
    entries = colors_to_entries(colors, rle_tolerance=rle_tolerance)
    chunks: list[bytes] = []
    set_id = set_id_start & 0xFF or 1
    for i in range(0, len(entries), ENTRIES_PER_CHUNK):
        chunk = entries[i : i + ENTRIES_PER_CHUNK]
        chunks.append(_build_section_packet(chunk, set_id))
        set_id = set_id + 1 if set_id < 254 else 1
    return chunks, set_id


def _build_section_packet(entries: list[Entry], set_id: int) -> bytes:
    payload_len = 5 * len(entries)
    total_len = 6 + payload_len
    buf = bytearray(total_len)
    buf[0] = ord("R")
    buf[1] = ord("B")
    buf[2] = total_len
    buf[3] = set_id & 0xFF
    buf[4] = ACTION_SECTION_LED
    off = 5
    for start, r, g, b, end in entries:
        buf[off] = start & 0xFF
        buf[off + 1] = r & 0xFF
        buf[off + 2] = g & 0xFF
        buf[off + 3] = b & 0xFF
        buf[off + 4] = end & 0xFF
        off += 5
    buf[-1] = _checksum(bytes(buf[:-1]))
    return bytes(buf)
