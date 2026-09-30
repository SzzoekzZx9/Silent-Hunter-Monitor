#!/usr/bin/env python3
"""Full-memory snapshot & differential toolkit (story #12).

Finds live game state by diffing memory snapshots taken around a controlled
in-game action (e.g. moving a crewman between compartments).

Safety rules:
  * Snapshots stream region-by-region to disk (never tmpfs, never RAM).
  * The differ works per 4 KiB block with bounded counters, then examines
    only candidate blocks in detail. No unbounded in-memory hit lists.

Snapshot file format: repeated [u32 region_start][u32 region_size][raw bytes].

Usage:
  python3 mem_diff.py <pid> snap /path/to/snapshot.bin
  python3 mem_diff.py diff A.bin B.bin C.bin   # stable-in-AB, changed-in-AC
"""
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

CHUNK = 1 << 20
BLOCK = 0x1000  # 4 KiB analysis blocks
MAX_DETAIL_BLOCKS = 200  # examine this many candidate blocks in detail


def snap(pid, path):
    regions = list(utils.iter_rw_regions(pid))
    total = sum(e - s for s, e in regions)
    print(f'{len(regions)} regions, {total >> 20} MiB -> {path}')
    t0 = time.time()
    written = 0
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem, \
            open(path, 'wb') as out:
        for start, end in regions:
            size = end - start
            out.write(struct.pack('<II', start, size))
            addr = start
            while addr < end:
                try:
                    mem.seek(addr)
                    data = mem.read(min(CHUNK, end - addr))
                except (IOError, OSError):
                    data = b''
                if not data:
                    # unreadable gap: pad to keep the file aligned
                    data = b'\x00' * min(CHUNK, end - addr)
                out.write(data)
                written += len(data)
                addr += len(data)
            print(f'\r  {written >> 20}/{total >> 20} MiB', end='', flush=True)
    print(f'\ndone in {time.time() - t0:.0f}s')


def load_index(path):
    """Return {region_start: (size, file_offset_of_data, region_start)}."""
    idx = {}
    off = 0
    with open(path, 'rb') as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            start, size = struct.unpack('<II', hdr)
            idx[start] = (size, off + 8, start)
            f.seek(size, 1)
            off += 8 + size
    return idx


def read_region(f, entry, start, size):
    """Read `size` bytes of snapshot data at virtual address `start`.

    `entry` is (region_size, file_offset, region_start) from load_index.
    """
    rsize, roff, rstart = entry
    f.seek(roff + (start - rstart))
    return f.read(size)


def diff(sa, sb, sc):
    """Report blocks stable between A and B but changed between A and C."""
    ia, ib, ic = load_index(sa), load_index(sb), load_index(sc)
    fa, fb, fc = open(sa, 'rb'), open(sb, 'rb'), open(sc, 'rb')
    common = set(ia) & set(ib) & set(ic)
    print(f'{len(common)} regions common to all three snapshots')

    # pass 1: per-block change counters
    cand = {}  # block_addr -> (n_ac_changes, region_start)
    t0 = time.time()
    for rstart in sorted(common):
        size = min(ia[rstart][0], ib[rstart][0], ic[rstart][0])
        off = 0
        while off < size:
            n = min(BLOCK, size - off)
            da = read_region(fa, ia[rstart], rstart + off, n)
            db = read_region(fb, ib[rstart], rstart + off, n)
            dc = read_region(fc, ic[rstart], rstart + off, n)
            if da != dc:
                n_ac = sum(1 for i in range(0, n, 4)
                           if da[i:i+4] != dc[i:i+4])
                n_ab = sum(1 for i in range(0, n, 4)
                           if da[i:i+4] != db[i:i+4])
                if n_ac and n_ab == 0:
                    cand[rstart + off] = (n_ac, rstart)
            off += n
    print(f'pass 1 done in {time.time() - t0:.0f}s: '
          f'{len(cand)} candidate blocks')

    # pass 2: word-level detail. Prefer blocks with FEW changes (a struct
    # field edit) over full-block churn (graphics/UI). Filter for changes
    # that look like game state: small ints or [0,1] floats.
    small = [(b, n) for b, (n, r) in cand.items() if 0 < n <= 32]
    print(f'{len(small)} candidate blocks with 1-32 changed words')
    ranked = sorted(small, key=lambda kv: kv[1])[:MAX_DETAIL_BLOCKS]
    for block, n_ac in ranked:
        rstart = cand[block][1]
        size = min(ia[rstart][0], ib[rstart][0], ic[rstart][0])
        boff = block - rstart
        n = min(BLOCK, size - boff)
        da = read_region(fa, ia[rstart], block, n)
        dc = read_region(fc, ic[rstart], block, n)
        lines = []
        for i in range(0, n - 3, 4):
            wa, wc = da[i:i+4], dc[i:i+4]
            if wa != wc:
                va, vc = struct.unpack('<I', wa)[0], struct.unpack('<I', wc)[0]
                fa_f = struct.unpack('<f', wa)[0]
                fc_f = struct.unpack('<f', wc)[0]
                small_int = va < 0x1000 and vc < 0x1000
                unit_float = (0.0 <= fa_f <= 1.0 and 0.0 <= fc_f <= 1.0
                              and (fa_f != 0 or fc_f != 0))
                if small_int or unit_float:
                    note = ''
                    if unit_float and 0.0001 < abs(fa_f) < 100:
                        note = f'  (float {fa_f:.4f} -> {fc_f:.4f})'
                    lines.append(f'  0x{block + i:08x}: {va:08x} -> '
                                 f'{vc:08x}{note}')
        if lines:
            print(f'\n=== block 0x{block:08x} ({n_ac} words changed, '
                  f'{len(lines)} state-like) ===')
            for ln in lines[:16]:
                print(ln)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[2]
    if cmd == 'snap':
        snap(int(sys.argv[1]), sys.argv[3])
    elif cmd == 'diff':
        diff(sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()