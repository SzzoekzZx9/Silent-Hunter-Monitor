#!/usr/bin/env python3
"""
Drift scanner: find floats that change monotonically across 3 snapshots.

A boat cruising at a steady speed has a world position that drifts
monotonically (same sign delta each interval), unlike per-frame wobble
(speed, waves) or one-shot writes. This tool samples every rw page below
0x80000000 (the 32-bit game arena; above that is wine/proton runtime)
three times and reports float addresses whose value moved in the same
direction in both intervals.

MEMORY SAFETY: full page contents are never held in RAM. Passes 1-3
store only a 20-byte sha1 per page (~60MB for the whole arena). A second
phase then re-reads only the pages that changed in both intervals
(typically a few MB) to extract and compare the actual floats.

Usage:
  python3 drift_scan.py [pid] [interval_seconds]
"""
import sys
import os
import time
import struct
import hashlib

PAGE = 4096
ARENA_END = 0x80000000
MAX_CANDIDATE_PAGES = 20000  # hard cap on phase-2 pages (~80MB)

# Known landmarks for annotating hits.
LANDMARKS = [
    (0x00554698, "SPEED_PTR static ptr"),
    (0x005546d0, "GRID_PTR (stale, issue #17)"),
    (0x02c1f0000, 0x02c220000, "grid-name tables"),
    (0x03b3e0000, 0x03b4a8000, "parsed INI config (campaign)"),
]


def iter_rw_regions(pid):
    with open(f'/proc/{pid}/maps') as mf:
        for line in mf:
            parts = line.split()
            if 'rw' not in parts[1]:
                continue
            s, e = parts[0].split('-')
            s, e = int(s, 16), int(e, 16)
            if e <= ARENA_END:
                yield s, e


def digest_pass(pid):
    """Return {page_addr: sha1_digest} for every readable rw page."""
    pages = {}
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for start, end in iter_rw_regions(pid):
            for addr in range(start, end, PAGE):
                try:
                    mem.seek(addr)
                    buf = mem.read(PAGE)
                except OSError:
                    continue
                if len(buf) == PAGE:
                    pages[addr] = hashlib.sha1(buf).digest()
    return pages


def read_pages(pid, addrs):
    """Return {page_addr: bytes} for the given pages only."""
    out = {}
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for addr in addrs:
            try:
                mem.seek(addr)
                buf = mem.read(PAGE)
            except OSError:
                continue
            if len(buf) == PAGE:
                out[addr] = buf
    return out


def find_pid():
    best = None
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                with open(f'/proc/{d}/cmdline') as f:
                    if 'sh3.exe' in f.read():
                        rss = int(open(f'/proc/{d}/statm').read().split()[1])
                        if best is None or rss > best[1]:
                            best = (int(d), rss)
            except OSError:
                continue
    return best[0] if best else None


def landmark(addr):
    for lm in LANDMARKS:
        if len(lm) == 2:
            if lm[0] <= addr < lm[0] + 64:
                return lm[1]
        else:
            if lm[0] <= addr < lm[1]:
                return lm[2]
    return ''


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid()
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    if pid is None:
        print("sh3.exe not found", file=sys.stderr)
        sys.exit(1)

    print(f"pid {pid}, 3 digest passes, {interval:.0f}s apart "
          f"(~60MB RAM per pass, no page contents kept)")
    t0 = time.time()
    d1 = digest_pass(pid)
    print(f"pass 1: {len(d1)} pages ({time.time()-t0:.0f}s)")
    time.sleep(interval)
    d2 = digest_pass(pid)
    print(f"pass 2: {len(d2)} pages")
    time.sleep(interval)
    d3 = digest_pass(pid)
    print(f"pass 3: {len(d3)} pages")

    # Pages present in all three and changing in both intervals.
    changed_both = [a for a in d1
                    if a in d2 and a in d3
                    and d1[a] != d2[a] and d2[a] != d3[a]]
    print(f"\npages changing in both intervals: {len(changed_both)}")
    if len(changed_both) > MAX_CANDIDATE_PAGES:
        print(f"too many churn pages ({len(changed_both)}), "
              f"capping to first {MAX_CANDIDATE_PAGES} by address",
              file=sys.stderr)
        changed_both.sort()
        changed_both = changed_both[:MAX_CANDIDATE_PAGES]

    # Phase 2: re-read only candidate pages, three times.
    addrs = sorted(changed_both)
    b1 = read_pages(pid, addrs)
    time.sleep(interval)
    b2 = read_pages(pid, addrs)
    time.sleep(interval)
    b3 = read_pages(pid, addrs)

    candidates = []
    for addr in addrs:
        p1, p2, p3 = b1.get(addr), b2.get(addr), b3.get(addr)
        if p1 is None or p2 is None or p3 is None:
            continue
        f1 = struct.unpack_from('<1024f', p1, 0)
        f2 = struct.unpack_from('<1024f', p2, 0)
        f3 = struct.unpack_from('<1024f', p3, 0)
        for i in range(1024):
            v1, v2, v3 = f1[i], f2[i], f3[i]
            if v1 != v1 or v2 != v2 or v3 != v3:
                continue
            d1v, d2v = v2 - v1, v3 - v2
            if d1v == 0 or d2v == 0:
                continue
            # Monotonic drift, both deltas same sign, sane magnitudes,
            # roughly constant rate (within 5x) — filters most wobble.
            if (d1v > 0) != (d2v > 0):
                continue
            if abs(v3) > 1e9:
                continue
            r = abs(d2v / d1v) if d1v else 0
            if not (0.2 <= r <= 5.0):
                continue
            candidates.append((addr + i * 4, v1, v2, v3, d1v, d2v))

    print(f"\n{len(candidates)} monotonic drift floats:")
    from collections import defaultdict
    by_block = defaultdict(list)
    for addr, v1, v2, v3, d1v, d2v in candidates:
        by_block[addr >> 16].append((addr, v1, v2, v3, d1v, d2v))
    for blk in sorted(by_block):
        rows = by_block[blk]
        mark = landmark(blk << 16)
        print(f"\n-- block 0x{blk:08x}xxxx  ({len(rows)} floats)  {mark}")
        for addr, v1, v2, v3, d1v, d2v in rows[:40]:
            print(f"   0x{addr:08x}  {v1:16.4f} {v2:16.4f} {v3:16.4f}"
                  f"   d={d1v:+.6f}/{d2v:+.6f}")


if __name__ == '__main__':
    main()