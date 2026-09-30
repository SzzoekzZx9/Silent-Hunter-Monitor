#!/usr/bin/env python3
"""Differential scan for a live lat/long pair.

Pass A: collect all floats (and doubles) in plausible lat/lon ranges.
Wait. Pass B: re-read candidates, keep those whose value changed.

Ranges: lat [30, 70], lon [-20, 20] (degrees). Also radians: [0.5, 1.3].
Memory-safe: streams 4KB pages, keeps only candidate (addr, value) pairs.
"""
import struct
import sys
import time

PID = int(sys.argv[1])
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0


def rw_regions(pid):
    out = []
    for line in open(f'/proc/{pid}/maps'):
        if 'rw' not in line.split('(')[0].split()[-1] and ' rw' not in line:
            continue
        parts = line.split()
        if 'rw' not in parts[1]:
            continue
        a, b = (int(x, 16) for x in parts[0].split('-'))
        if b - a > 0x40000000:
            continue
        out.append((a, b))
    return out


def scan(pid, addrs=None):
    """If addrs is None: find candidates. Else: re-read them."""
    mem = open(f'/proc/{pid}/mem', 'rb', buffering=0)
    found = {}
    if addrs is None:
        for a, b in rw_regions(pid):
            for page in range(a, b, 4096):
                try:
                    mem.seek(page)
                    buf = mem.read(4096)
                except OSError:
                    continue
                for off in range(0, 4092, 4):
                    v, = struct.unpack_from('<f', buf, off)
                    if (30.0 <= v <= 70.0) or (-20.0 <= v <= 20.0 and v != 0.0) \
                       or (0.5 <= v <= 1.3):
                        found[page + off] = v
    else:
        for addr in addrs:
            try:
                mem.seek(addr)
                found[addr] = struct.unpack('<f', mem.read(4))[0]
            except OSError:
                pass
    mem.close()
    return found


t0 = time.time()
print(f"pass A: scanning for degree-range floats...", flush=True)
cand = scan(PID)
print(f"pass A done: {len(cand)} candidates in {time.time()-t0:.0f}s", flush=True)
print(f"waiting {WAIT:.0f}s ...", flush=True)
time.sleep(WAIT)
print("pass B: re-reading candidates ...", flush=True)
t1 = time.time()
cur = scan(PID, list(cand.keys()))
print(f"pass B done in {time.time()-t1:.0f}s", flush=True)

moved = []
for addr, old in cand.items():
    new = cur.get(addr)
    if new is None or new == old:
        continue
    d = new - old
    if 1e-7 <= abs(d) <= 0.5:
        moved.append((addr, old, new, d))

print(f"\n*** {len(moved)} MOVING degree-range floats ***")
for addr, old, new, d in sorted(moved):
    print(f"0x{addr:08x}: {old:12.7f} -> {new:12.7f}  (d={d:+.7f})")