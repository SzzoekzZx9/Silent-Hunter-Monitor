#!/usr/bin/env python3
"""Background watcher for issue #17.

Two jobs, both memory-safe (page digests only, no snapshots held):
1. Sample the live boat position pair (0x3b529d40/48) + heading/speed every 0.5s
   -> /tmp/grid_watch.csv   (wall_ts, x, z, hdg, spd)
   Rebase jumps are visible in the CSV as large deltas.
2. Every DIGEST_INTERVAL seconds, digest all rw pages < 0x80000000 and log which
   pages changed during each interval
   -> /tmp/grid_watch_intervals.log  (one line per interval: ts n addr addr ...)

Later analysis: pages that changed in the interval containing a grid crossing but
NOT in cruising intervals are candidates for the game's internal grid ID.

Usage: python3 -u grid_watch.py [pid]
"""
import hashlib
import os
import struct
import sys
import time

PID = int(sys.argv[1]) if len(sys.argv) > 1 else 2264
POS = 0x3b529d40
SPEED_PTR = 0x554698
ARENA_END = 0x80000000
PAGE = 4096
DIGEST_INTERVAL = 45.0
SAMPLE_INTERVAL = 0.5
CSV = '/tmp/grid_watch.csv'
INTLOG = '/tmp/grid_watch_intervals.log'


def rw_pages():
    pages = []
    with open(f'/proc/{PID}/maps') as mf:
        for line in mf:
            p = line.split()
            if 'rw' in p[1]:
                s, e = p[0].split('-')
                s, e = int(s, 16), int(e, 16)
                if e <= ARENA_END:
                    pages.extend(range(s, e, PAGE))
    return pages


def digest_all(pages):
    digests = {}
    with open(f'/proc/{PID}/mem', 'rb', buffering=0) as m:
        for addr in pages:
            try:
                m.seek(addr)
                buf = m.read(PAGE)
            except OSError:
                continue
            digests[addr] = hashlib.sha1(buf).digest()
    return digests


def sample(mem):
    try:
        mem.seek(SPEED_PTR)
        base = struct.unpack('<I', mem.read(4))[0]
        mem.seek(base + 0x64)
        hdg, spd = struct.unpack('<2f', mem.read(8))
        mem.seek(POS)
        x, z = struct.unpack('<2f', mem.read(8))
        return x, z, hdg, spd
    except (OSError, struct.error):
        return None


def main():
    print(f"pid={PID} watching POS=0x{POS:08x} digest every {DIGEST_INTERVAL}s")
    pages = rw_pages()
    print(f"arena: {len(pages)} pages ({len(pages)*PAGE//1024//1024} MB)")
    prev_digests = None
    interval_start = time.time()
    next_digest = time.time() + DIGEST_INTERVAL

    csv = open(CSV, 'a', buffering=1)
    intlog = open(INTLOG, 'a', buffering=1)

    mem = open(f'/proc/{PID}/mem', 'rb', buffering=0)
    last = None
    while True:
        now = time.time()
        s = sample(mem)
        if s is None:
            print("read failed (game closed?); exiting")
            break
        x, z, hdg, spd = s
        csv.write(f"{now:.1f},{x:.3f},{z:.3f},{hdg:.2f},{spd:.2f}\n")
        if last is not None:
            dx, dz = x - last[0], z - last[1]
            if abs(dx) > 60 or abs(dz) > 60:
                print(f"JUMP t={now:.1f} d=({dx:+.1f},{dz:+.1f}) -> ({x:.1f},{z:.1f}) [rebase?]")
        last = (x, z)

        if now >= next_digest:
            digests = digest_all(pages)
            if prev_digests is not None:
                changed = [a for a, d in digests.items()
                           if prev_digests.get(a) != d]
                intlog.write(f"{interval_start:.1f} {now:.1f} {len(changed)} " +
                             " ".join(f"{a:x}" for a in changed) + "\n")
                intlog.flush()
                print(f"interval {interval_start:.0f}-{now:.0f}: {len(changed)} pages changed")
            prev_digests = digests
            interval_start = now
            next_digest = now + DIGEST_INTERVAL
            # refresh map in case it changed
            try:
                pages = rw_pages()
            except OSError:
                print("maps read failed; exiting")
                break
        time.sleep(SAMPLE_INTERVAL)


if __name__ == '__main__':
    main()