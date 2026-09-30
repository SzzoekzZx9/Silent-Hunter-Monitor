#!/usr/bin/env python3
"""Differential snapshot tool for SH3 static data regions (story #12).

Used to locate the crew "slot map" — the structure that tracks which crewman
occupies which station/slot on the in-game crew screen.

Method:
  1. snapshot  — capture the small static EXE data regions (~4 MB) to a file
  2. (user performs one controlled crew move in game)
  3. diff      — compare live memory against the snapshot, classify changes

Classification rules learned from experiments:
  - Roster table 0x5F6154..+140*0xE4: fatigue (+0x54) ticks constantly; whole
    entries get swapped/reordered by the game, so per-entry field changes are
    expected on any crew move and are NOT the slot map.
  - 0x603000-0x606000: physics/animation churn (small signed floats), noise.
  - Everything else that changes on a crew move is a slot-map candidate.

Memory safety: reads only the two small static regions (~4 MB total), streams
per-region, never touches large heap/graphics regions.

Usage:
  static_diff.py snapshot <pid> <outfile>
  static_diff.py diff <pid> <snapshotfile> [--max-print N]

Snapshot file format: repeated [u32 region_start][u32 region_size][raw bytes].
"""
import argparse
import struct
import sys

# sh3.exe data sections + adjacent static data (from /proc/<pid>/maps)
REGIONS = [(0x544000, 0x632000), (0x641000, 0x940000)]

ROSTER_BASE = 0x5F6154
ROSTER_STRIDE = 0xE4
ROSTER_COUNT = 140
PHYSICS_NOISE = (0x603000, 0x606000)


def read_region(pid, lo, hi, chunk=1 << 16):
    """Read [lo,hi) in chunks. Returns (data, failed_ranges).

    Failed/short chunks are zero-filled in `data` and their ranges are
    reported in `failed_ranges` so callers can skip them.
    """
    out = bytearray()
    failed = []
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        pos = lo
        while pos < hi:
            n = min(chunk, hi - pos)
            try:
                mem.seek(pos)
                data = mem.read(n)
            except OSError:
                data = b''
            if len(data) != n:
                failed.append((pos, pos + n))
                data = b'\x00' * n
            out += data
            pos += n
    return bytes(out), failed


def cmd_snapshot(pid, outfile):
    total = 0
    with open(outfile, 'wb') as f:
        for lo, hi in REGIONS:
            data, failed = read_region(pid, lo, hi)
            for a, b in failed:
                print(f'warning: unreadable 0x{a:08x}-0x{b:08x}', file=sys.stderr)
            f.write(lo.to_bytes(4, 'little'))
            f.write(len(data).to_bytes(4, 'little'))
            f.write(data)
            total += len(data)
    print(f'saved {outfile}: {total} bytes in {len(REGIONS)} regions')


def load_snapshot(path):
    regions = []
    with open(path, 'rb') as f:
        blob = f.read()
    pos = 0
    while pos < len(blob):
        lo = int.from_bytes(blob[pos:pos + 4], 'little')
        sz = int.from_bytes(blob[pos + 4:pos + 8], 'little')
        regions.append((lo, blob[pos + 8:pos + 8 + sz]))
        pos += 8 + sz
    return regions


def cmd_diff(pid, snapfile, max_print):
    regions = load_snapshot(snapfile)
    roster_lo = ROSTER_BASE
    roster_hi = ROSTER_BASE + ROSTER_COUNT * ROSTER_STRIDE

    roster_changes = {}   # entry index -> list of (offset, old, new)
    other = []
    total = 0
    for lo, old in regions:
        new, failed = read_region(pid, lo, lo + len(old))
        for a, b in failed:
            print(f'warning: skipping unreadable 0x{a:08x}-0x{b:08x}', file=sys.stderr)
        for i in range(0, len(old) - 3, 4):
            if old[i:i + 4] == new[i:i + 4]:
                continue
            total += 1
            addr = lo + i
            if roster_lo <= addr < roster_hi:
                entry, off = divmod(addr - roster_lo, ROSTER_STRIDE)
                roster_changes.setdefault(entry, []).append(
                    (off, old[i:i + 4], new[i:i + 4]))
            elif PHYSICS_NOISE[0] <= addr < PHYSICS_NOISE[1]:
                pass
            else:
                other.append((addr, old[i:i + 4], new[i:i + 4]))

    print(f'total changed words: {total}')
    print(f'\n=== non-roster, non-physics changes ({len(other)}) — slot-map candidates ===')
    for addr, o, n in other[:max_print]:
        vo, vn = struct.unpack('<I', o)[0], struct.unpack('<I', n)[0]
        fo, fn = struct.unpack('<f', o)[0], struct.unpack('<f', n)[0]
        fs = ''
        if 0.001 < abs(fo) < 100000 or 0.001 < abs(fn) < 100000:
            fs = f'  f:{fo:.4f}->{fn:.4f}'
        print(f'  0x{addr:08x}: {vo:08x} -> {vn:08x}{fs}')
    if len(other) > max_print:
        print(f'  ... ({len(other) - max_print} more)')

    print(f'\n=== roster entries with non-fatigue changes ({len(roster_changes)}) ===')
    # current names for context
    names = {}
    try:
        data, _ = read_region(pid, roster_lo, roster_lo + ROSTER_COUNT * ROSTER_STRIDE)
        for n in range(ROSTER_COUNT):
            e = data[n * ROSTER_STRIDE:(n + 1) * ROSTER_STRIDE]
            z = e.find(b'\x00')
            if 0 < z <= 24:
                nm = e[:z].decode('ascii', 'replace')
                if nm.isprintable() and len(nm) > 3:
                    names[n] = nm
    except OSError:
        pass
    for entry, lst in sorted(roster_changes.items()):
        nonfat = [x for x in lst if x[0] != 0x54]
        if not nonfat:
            continue
        print(f'  [{entry:3}] {names.get(entry, "?")[:20]:<20}')
        for off, o, n in nonfat:
            vo, vn = struct.unpack('<I', o)[0], struct.unpack('<I', n)[0]
            print(f'      +0x{off:03x}: {vo:08x} -> {vn:08x}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p1 = sub.add_parser('snapshot')
    p1.add_argument('pid', type=int)
    p1.add_argument('outfile')
    p2 = sub.add_parser('diff')
    p2.add_argument('pid', type=int)
    p2.add_argument('snapfile')
    p2.add_argument('--max-print', type=int, default=200)
    args = ap.parse_args()
    if args.cmd == 'snapshot':
        cmd_snapshot(args.pid, args.outfile)
    else:
        cmd_diff(args.pid, args.snapfile, args.max_print)


if __name__ == '__main__':
    main()