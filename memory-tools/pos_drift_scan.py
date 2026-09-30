#!/usr/bin/env python3
"""Memory-safe position drift scanner for SH3 (issue #17).

Waits for the game to be running, then:
  pass 1: sha1-digest every rw page below ARENA_END          (~60 MB RAM)
  wait INTERVAL
  pass 2: digest again -> set S of pages that changed
  read content of S pages (snapshot A), wait INTERVAL, read again (B)
  report floats/doubles that drifted A->B, grouped and annotated.

The boat's world position (wherever it lives) changes every tick while
the boat cruises, so it must appear in S and keep drifting A->B.
Everything static (coastlines, configs, name tables) is filtered out.

Usage: sudo python3 pos_drift_scan.py [pid] [interval_seconds]
"""
import hashlib
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils  # noqa: E402

ARENA_END = 0x80000000
PAGE = 4096
# landmarks for annotation (high 16 bits of address -> label)
LANDMARKS = [
    (0x00554000, 'static-ptr-block (SPEED_PTR/GRID_PTR)'),
    (0x01d2f000, 'DEPTH_PTR block'),
    (0x02c1f000, 'grid name tables'),
    (0x03b3e000, 'parsed INI config'),
    (0x04ed50000 >> 4 << 4, 'audio/DSP block'),  # 0x4ed50000
]


def rw_pages(pid):
    out = []
    with open(f'/proc/{pid}/maps') as mf:
        for line in mf:
            p = line.split()
            if 'rw' not in p[1]:
                continue
            s, e = p[0].split('-')
            s, e = int(s, 16), int(e, 16)
            if e <= ARENA_END:
                out.append((s, e))
    return out


def digest_all(pid, pages):
    """Return {page_addr: sha1_digest} for every page."""
    digests = {}
    h = hashlib.sha1
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for s, e in pages:
            for addr in range(s, e, PAGE):
                try:
                    mem.seek(addr)
                    buf = mem.read(PAGE)
                except OSError:
                    continue
                if len(buf) == PAGE:
                    digests[addr] = h(buf).digest()
    return digests


def read_pages(pid, addrs):
    """Return {page_addr: bytes} for the given pages."""
    out = {}
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for addr in addrs:
            try:
                mem.seek(addr)
                out[addr] = mem.read(PAGE)
            except OSError:
                pass
    return out


def wait_for_running(pid, timeout=1800):
    """Block until the game sim is advancing (in-game clock ticking)."""
    print("waiting for game clock to advance...")
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            c = utils.find_ui_clock(pid)
        except Exception:
            c = None
        if c is not None and last is not None and c != last:
            print("game is running.")
            return True
        last = c
        time.sleep(2)
    return False


def annotate(addr):
    for base, label in LANDMARKS:
        if base <= addr < base + 0x100000:
            return label
    return ''


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else utils.get_pid_by_name()
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    print(f"pid={pid} interval={interval}s")

    if not wait_for_running(pid):
        print("game never unpaused; aborting")
        return

    pages = rw_pages(pid)
    total = sum(e - s for s, e in pages)
    print(f"arena: {len(pages)} ranges, {total/1e6:.0f} MB")

    print("pass 1 digest...")
    d1 = digest_all(pid, pages)
    time.sleep(interval)
    print("pass 2 digest...")
    d2 = digest_all(pid, pages)
    changed = sorted(a for a in d1 if a in d2 and d1[a] != d2[a])
    print(f"changed pages: {len(changed)}")
    if not changed:
        print("nothing changed - game paused again during scan?")
        return

    print("snapshot A...")
    a = read_pages(pid, changed)
    time.sleep(interval)
    print("snapshot B...")
    b = read_pages(pid, changed)

    # collect drifting floats
    hits = []  # (addr, va, vb, delta)
    for addr in changed:
        ba, bb = a.get(addr), b.get(addr)
        if ba is None or bb is None or ba == bb:
            continue
        fa = struct.unpack(f'<{PAGE//4}f', ba)
        fb = struct.unpack(f'<{PAGE//4}f', bb)
        for i in range(PAGE // 4):
            va, vb = fa[i], fb[i]
            if va == vb or va != va or vb != vb:
                continue
            if abs(vb) > 1e12 or abs(va) < 1e-6:
                continue
            hits.append((addr + i * 4, va, vb, vb - va))

    print(f"drifting floats: {len(hits)}")

    # pair detection: two drifting floats within 32 bytes of each other
    byaddr = {h[0]: h for h in hits}
    pairs = []
    for addr, va, vb, dv in hits:
        for off in (4, 8, 12, 16, 20, 24, 28, 32):
            if addr + off in byaddr:
                pairs.append((addr, off, byaddr[addr], byaddr[addr + off]))

    # group singles by 64KB block
    from collections import defaultdict
    blocks = defaultdict(list)
    for h in hits:
        blocks[h[0] >> 16].append(h)

    print(f"\n=== DRIFTING PAIRS (candidate x/z positions) ===")
    for addr, off, h1, h2 in sorted(pairs):
        a1, va1, vb1, d1_ = h1
        a2, va2, vb2, d2_ = h2
        note = annotate(addr)
        print(f"0x{a1:08x}+{off}: "
              f"({va1:.4f}->{vb1:.4f}, d={d1_:+.4f}) & "
              f"({va2:.4f}->{vb2:.4f}, d={d2_:+.4f}) {note}")

    print(f"\n=== DRIFTING FLOATS BY 64KB BLOCK ===")
    for blk in sorted(blocks):
        rows = blocks[blk]
        note = annotate(blk << 16)
        print(f"-- 0x{blk:08x}xxxx ({len(rows)}) {note}")
        for addr, va, vb, dv in rows[:8]:
            print(f"   0x{addr:08x}: {va:.4f} -> {vb:.4f}  (d={dv:+.5f}/int)")


if __name__ == '__main__':
    main()