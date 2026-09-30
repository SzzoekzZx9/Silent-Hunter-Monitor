#!/usr/bin/env python3
"""Crew-record diff watcher (story #12 - crew fatigue collector discovery).

SH3 stores crew members as text-serialized property records
("CREW_MEMBER_N" ... Name / Index / Qual / Medal / HealthStatus / ...)
in many heap copies. To find which copies are LIVE (updated as fatigue
changes with in-game time) this tool:

1. scans memory for all b"CREW_MEMBER_" seeds
2. snapshots a window around each seed
3. re-reads after an interval and reports every changed window,
   with the changed bytes in context (old -> new)

Usage: python3 crew_diff.py <pid> [interval_seconds] [window_bytes]
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

SCAN_START = 0x08048000
SCAN_END = 0xC0000000
WINDOW = 0x400  # bytes around each seed, both directions


def snapshot(pid, centers, window):
    """Read window bytes around each center; returns {center: bytes}."""
    snaps = {}
    with open(f'/proc/{pid}/mem', 'rb') as mem:
        for c in centers:
            start = c - window
            try:
                mem.seek(start)
                snaps[c] = mem.read(2 * window)
            except (IOError, OSError):
                continue
    return snaps


def context(data, off, radius=48):
    """Printable context around a change offset, with a marker."""
    lo = max(0, off - radius)
    hi = min(len(data), off + radius)
    chunk = data[lo:hi]
    text = ''.join(chr(b) if 0x20 <= b < 0x7f else '.' for b in chunk)
    marker = ' ' * (off - lo) + '####'
    return text, marker


def main():
    pid = int(sys.argv[1])
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
    window = int(sys.argv[3], 0) if len(sys.argv) > 3 else WINDOW

    seeds = utils.fast_seed_scan(pid, SCAN_START, SCAN_END, b'CREW_MEMBER_')
    # dedupe overlapping windows
    centers = []
    for s in sorted(seeds):
        if not centers or s - centers[-1] > 2 * window:
            centers.append(s)
    print(f"{len(seeds)} seeds -> {len(centers)} watch windows "
          f"(+/-{window:#x}), interval {interval:.0f}s")

    before = snapshot(pid, centers, window)
    print(f"snapshot A taken at {time.strftime('%H:%M:%S')}, "
          f"waiting {interval:.0f}s (let in-game time pass)...")
    time.sleep(interval)
    after = snapshot(pid, centers, window)
    print(f"snapshot B taken at {time.strftime('%H:%M:%S')}")

    changed = 0
    for c in centers:
        a, b = before.get(c), after.get(c)
        if a is None or b is None or a == b:
            continue
        changed += 1
        print(f"\n===== window @ 0x{c:08x} CHANGED =====")
        # report changed 4-byte words
        i = 0
        shown = 0
        while i < len(a) and shown < 40:
            if a[i:i+4] != b[i:i+4]:
                old, new = a[i:i+4], b[i:i+4]
                text, marker = context(b, i)
                print(f"  +{i - window:+#06x} (0x{c - window + i:08x}): "
                      f"{old.hex()} -> {new.hex()}")
                print(f"    new ctx |{text}|")
                print(f"           |{marker}|")
                shown += 1
                i += 4
            else:
                i += 4
    if changed == 0:
        print("\nNo windows changed. Either no in-game time passed, or all "
              "copies are stale serializations.")
    else:
        print(f"\n{changed}/{len(centers)} windows changed")


if __name__ == '__main__':
    main()