#!/usr/bin/env python3
"""SAFE float-pair scanner for story #12 (live crew fatigue discovery).

Scans RW regions of the SH3 process for memory windows containing floats
matching TWO different signatures simultaneously (e.g. one crewman at ~15%
fatigue and another at ~95%), which strongly indicates a live crew-state
structure.

Safety rules (learned the hard way — an unbounded scan OOM'd the machine):
  * Streams the address space in 64 KiB chunks; never holds more than one
    chunk plus a small carry buffer in memory.
  * Hard cap on collected pairs (PAIR_CAP). Aborts loudly if exceeded.
  * Per-chunk candidate lists are discarded immediately after pairing.
  * Dry-run mode reports region count and total bytes before any reading.
  * C-speed byte prefilter: only words whose high byte is 0x3D/0x3E/0x3F
    (float exponents covering ~0.03-2.0) are unpacked and range-checked.

Usage:
  python3 float_pair_scan.py <pid> --dry-run
  python3 float_pair_scan.py <pid> [--radius 0x2000]
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

CHUNK = 0x10000          # 64 KiB read chunks
RADIUS = 0x2000          # pair members must be within this distance
PAIR_CAP = 20000         # abort if more pairs than this accumulate

# Buckets as uint32 bit patterns (positive IEEE754 floats compare as ints).
# A: [0.12, 0.18]  — crewman A ~15% fatigue
# B: [0.93, 1.001] — crewman B ~95-100% fatigue
BUCKETS = {
    'A': (0x3DF5C28F, 0x3E3851EC),
    'B': (0x3F6E147B, 0x3F804000),
}
BLOCK_PAIR_CAP = 64  # blocks with more pairs than this are noise (tables)
HIGH_BYTES = b'\x3d\x3e\x3f'  # exponents covering the bucket ranges


def iter_regions(pid):
    return list(utils.iter_rw_regions(pid))


def dry_run(pid):
    regions = iter_regions(pid)
    total = sum(e - s for s, e in regions)
    print(f'{len(regions)} RW regions, {total / (1 << 20):.0f} MiB total')
    for s, e in sorted(regions, key=lambda r: r[1] - r[0], reverse=True)[:10]:
        print(f'  0x{s:08x}-0x{e:08x}  {(e - s) >> 20} MiB')
    return regions, total


def scan(pid, radius):
    regions = iter_regions(pid)
    total = sum(e - s for s, e in regions)
    print(f'scanning {len(regions)} regions, {total / (1 << 20):.0f} MiB '
          f'(radius {radius:#x}, pair cap {PAIR_CAP})')
    pairs = []
    noisy_blocks = 0
    t0 = time.time()
    read = 0
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for rstart, rend in regions:
            carry_a, carry_b = [], []
            addr = rstart
            while addr < rend:
                try:
                    mem.seek(addr)
                    data = mem.read(min(CHUNK, rend - addr))
                except (IOError, OSError):
                    break
                read += len(data)
                nwords = len(data) // 4
                if nwords:
                    hb = data[3::4]  # high byte of each aligned LE word
                    cur_a, cur_b = [], []
                    for hb_val in HIGH_BYTES:
                        pos = hb.find(hb_val)
                        while pos != -1:
                            w = int.from_bytes(
                                data[pos * 4:pos * 4 + 4], 'little')
                            lo, hi = BUCKETS['A']
                            if lo <= w <= hi:
                                cur_a.append((addr + pos * 4,
                                              struct.unpack(
                                                  '<f', data[pos*4:pos*4+4])[0]))
                            else:
                                lo, hi = BUCKETS['B']
                                if lo <= w <= hi:
                                    cur_b.append(
                                        (addr + pos * 4,
                                         struct.unpack(
                                             '<f', data[pos*4:pos*4+4])[0]))
                            pos = hb.find(hb_val, pos + 1)
                    # pair A-hits with B-hits (cur + carry) within radius
                    block = (addr + len(data)) >> 16
                    block_pairs = []
                    for a_addr, a_val in cur_a:
                        for b_addr, b_val in carry_b + cur_b:
                            if abs(a_addr - b_addr) <= radius:
                                block_pairs.append(
                                    (a_addr, a_val, b_addr, b_val))
                    if len(block_pairs) > BLOCK_PAIR_CAP:
                        noisy_blocks += 1  # coefficient table, drop it
                    else:
                        pairs.extend(block_pairs)
                        if len(pairs) > PAIR_CAP:
                            print(f'\nABORT: pair cap {PAIR_CAP} exceeded — '
                                  'narrow the buckets')
                            return pairs
                    cutoff = addr + len(data) - radius
                    carry_a = [(a, v) for a, v in cur_a if a >= cutoff]
                    carry_b = [(b, v) for b, v in cur_b if b >= cutoff]
                addr += len(data)
            print(f'\r  0x{rstart:08x} read {read / (1 << 20):.0f}/'
                  f'{total / (1 << 20):.0f} MiB, {len(pairs)} pairs',
                  end='', flush=True)
    print(f'\ndone in {time.time() - t0:.0f}s, {len(pairs)} pairs '
          f'({noisy_blocks} noisy blocks skipped)')
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pid', type=int)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--radius', type=lambda x: int(x, 0), default=RADIUS)
    args = ap.parse_args()
    if args.dry_run:
        dry_run(args.pid)
        return
    pairs = scan(args.pid, args.radius)
    from collections import defaultdict
    blocks = defaultdict(list)
    for a, av, b, bv in pairs:
        blocks[(a + b) // 2 >> 16].append((a, av, b, bv))
    print(f'\n{len(blocks)} 64KiB blocks with pairs:')
    for blk in sorted(blocks, key=lambda k: -len(blocks[k]))[:30]:
        ps = blocks[blk]
        print(f'  block 0x{blk << 16:08x}: {len(ps)} pairs, '
              f'e.g. A@0x{ps[0][0]:08x}={ps[0][1]:.3f} '
              f'B@0x{ps[0][2]:08x}={ps[0][3]:.3f}')


if __name__ == '__main__':
    main()