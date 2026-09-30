#!/usr/bin/env python3
"""Analyze grid_watch interval logs to isolate the internal grid ID.

Finds pages that changed during the interval containing a grid crossing but NOT
during any baseline (cruising) interval, then inspects those pages for grid-like
data: grid strings, pointers into the known grid tables, small ints, lat/long floats.

Usage:
  python3 crossing_diff.py <pid> <crossing_wall_ts> [--baseline all|after|before|both]

  crossing_wall_ts  approximate unix time of the grid crossing
  --baseline        which intervals to treat as cruising baseline
                    all (default) = every interval except the crossing one
"""
import argparse
import re
import struct
import sys

INTLOG = '/tmp/grid_watch_intervals.log'

# known static grid-table regions (from investigation notes)
GRID_TABLES = [
    (0x2c1f0000, 0x2c220000),   # grid-name tables
    (0x3b3e0000, 0x3b470000),   # Flotilla.cfg records
]

GRID_RE = re.compile(rb'[A-Z]{2}\d{2}\x00')


def load_intervals():
    intervals = []
    for line in open(INTLOG):
        parts = line.split()
        t0, t1, n = float(parts[0]), float(parts[1]), int(parts[2])
        pages = [int(p, 16) for p in parts[3:3 + n]]
        intervals.append((t0, t1, pages))
    return intervals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pid', type=int)
    ap.add_argument('crossing_ts', type=float)
    ap.add_argument('--baseline', default='all',
                    choices=['all', 'after', 'before'])
    args = ap.parse_args()

    intervals = load_intervals()
    if not intervals:
        sys.exit("no intervals logged yet")

    # find interval containing the crossing time
    cross = None
    for i, (t0, t1, pages) in enumerate(intervals):
        if t0 <= args.crossing_ts <= t1:
            cross = i
            break
    if cross is None:
        # fall back: first interval starting after the crossing time
        for i, (t0, t1, pages) in enumerate(intervals):
            if t0 > args.crossing_ts:
                cross = i
                break
    if cross is None:
        sys.exit(f"no interval found near ts {args.crossing_ts}")

    t0, t1, cross_pages = intervals[cross]
    print(f"crossing interval #{cross}: {t0:.0f}-{t1:.0f} ({len(cross_pages)} pages)")

    # baseline pages
    base = set()
    used = 0
    for i, (a, b, pages) in enumerate(intervals):
        if i == cross:
            continue
        if args.baseline == 'after' and b <= t1:
            continue
        if args.baseline == 'before' and a >= t0:
            continue
        base.update(pages)
        used += 1
    print(f"baseline: {used} intervals, {len(base)} pages total")

    cands = [p for p in cross_pages if p not in base]
    print(f"candidates (changed on crossing, stable while cruising): {len(cands)}")

    # group by 64KB block
    blocks = {}
    for p in cands:
        blocks.setdefault(p >> 16, []).append(p)
    for b in sorted(blocks):
        print(f"  block 0x{b:04x}xxxx: {len(blocks[b])} pages: " +
              ", ".join(f"0x{p:08x}" for p in sorted(blocks[b])[:12]))

    # inspect candidate pages
    print("\n--- page contents ---")
    with open(f'/proc/{args.pid}/mem', 'rb', buffering=0) as m:
        for p in sorted(cands):
            try:
                m.seek(p)
                buf = m.read(4096)
            except OSError:
                continue
            notes = []
            # grid strings
            for g in set(GRID_RE.findall(buf)):
                notes.append(f"grid '{g[:4].decode()}'")
            # pointers into grid tables
            ints = struct.unpack('<1024I', buf)
            ptrs = [v for v in ints
                    if any(lo <= v < hi for lo, hi in GRID_TABLES)]
            if ptrs:
                notes.append("table ptrs: " + ", ".join(f"0x{v:08x}" for v in ptrs[:6]))
            # lat/long-ish floats
            fl = struct.unpack('<1024f', buf)
            ll = [v for v in fl
                  if v == v and (40 <= v <= 72 or -25 <= v <= 25)
                  and abs(v) > 0.001]
            if ll:
                notes.append(f"lat/long-ish floats x{len(ll)}: " +
                             ", ".join(f"{v:.4f}" for v in ll[:8]))
            tag = "; ".join(notes) if notes else "(nothing obvious)"
            print(f"0x{p:08x}: {tag}")


if __name__ == '__main__':
    main()