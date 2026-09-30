#!/usr/bin/env python3
"""Live grid collector + boat position reader for issue #17.

The game world is EQUIRECTANGULAR: 1 degree of lat/lon = exactly 120 km
(see SubSim radioroom post p=2151079).  The boat's true position in that
world is stored as adjacent (x, z) doubles in units of 1/120000 degree:

    lat = z / 120000        lon_east = x / 120000

IMPORTANT (verified live, two sessions): the game's OWN grid string at
0x53cfec is computed from a DIFFERENT, wrongly-projected position (the
game "incorrectly calculates the coordinates" - the display string can
differ from the map by more than a full quadrat, e.g. string 'BE 33'
while the boat is in BE 32 on the map).  The string must NOT be used as
ground truth for the boat's position.

The TRUE (map) position is reached via a static pointer chain that is
stable across sessions (same exe):

    p1 = [0x93f644]           (static data)
    p2 = [p1 + 0x88]          (player position object)
    x  = double [p2 + 0x18], z = double [p2 + 0x20]

Verified against the in-game map: BE 34, BE 31 and BE 32 in successive
sessions, matching the player's reported quadrat each time.

Legacy hot-page scan machinery (digest/hot_pages/scan_pairs) is kept for
diagnostics; find_boat() matches pairs against the game string and is
therefore UNRELIABLE for the boat (see above).

Usage:
  python3 grid_collector.py             # resolve once and print
  python3 grid_collector.py --watch     # poll: position + grid + string
  python3 grid_collector.py 51.2 15.0   # resolve a manual position (lon_w)
"""
import os
import re
import struct
import subprocess
import sys
import time
import zlib

from grid_transform import compute_grid, LAT_MIN, LAT_MAX

GRID_STR = 0x53cfec           # game's own (wrongly calculated) grid string
BOAT_ROOT = 0x93f644          # static root of the true-position chain
VALID = re.compile(rb'^[A-Z]{2} \d{1,4}$')
DEG = 120000.0
LON_MIN, LON_MAX = -30.0, 101.0     # lon_w bounds of the grid system
CHUNK = 4 * 1024 * 1024


def find_pid():
    """The sh3.exe process that owns the grid string (there may be two)."""
    out = subprocess.run(['pgrep', '-f', 'sh3.exe'], capture_output=True, text=True)
    for pid in (int(x) for x in out.stdout.split()):
        try:
            with open(f'/proc/{pid}/mem', 'rb', buffering=0) as m:
                m.seek(GRID_STR)
                if VALID.match(m.read(16).split(b'\x00')[0]):
                    return pid
        except OSError:
            continue
    raise SystemExit('no sh3.exe process with a grid string found')


def read_grid(mem):
    """The game's own grid string (WRONG calc - reference only)."""
    mem.seek(GRID_STR)
    raw = mem.read(16).split(b'\x00')[0]
    return raw.decode() if VALID.match(raw) else None


def read_boat(mem):
    """The boat's TRUE (map) position via the static chain.

    Returns (lat, lon_w) or None when the chain is broken or the values
    are not a plausible in-grid position (e.g. main menu).
    """
    try:
        mem.seek(BOAT_ROOT)
        p1, = struct.unpack('<I', mem.read(4))
        if p1 < 0x10000:
            return None
        mem.seek(p1 + 0x88)
        p2, = struct.unpack('<I', mem.read(4))
        if p2 < 0x10000:
            return None
        mem.seek(p2 + 0x18)
        x, z = struct.unpack('<dd', mem.read(16))
    except OSError:
        return None
    lat, lon_w = z / DEG, -x / DEG
    if plausible(lat, lon_w):
        return lat, lon_w
    return None


def read_pair(mem, addr):
    mem.seek(addr)
    x, z = struct.unpack('<dd', mem.read(16))
    return z / DEG, -x / DEG          # lat, lon_w


def plausible(lat, lon_w):
    return LAT_MIN < lat < LAT_MAX and LON_MIN < lon_w < LON_MAX


# ------------------------------------------------------- legacy scan machinery

def rw_pages(pid):
    """Yield (start, end) rw ranges below 2 GB, chunk-bounded."""
    with open(f'/proc/{pid}/maps') as mf:
        for l in mf:
            p = l.split()
            if 'rw' in p[1]:
                s, e = (int(v, 16) for v in p[0].split('-'))
                if e <= 0x80000000 and e > s:
                    yield s, e


def digest(mem, pid):
    """crc32 per 4 KB page of every rw range. Returns {page: crc}."""
    d = {}
    buf = bytearray(CHUNK)
    view = memoryview(buf)
    for s, e in rw_pages(pid):
        for off in range(s, e, CHUNK):
            n = min(CHUNK, e - off)
            try:
                mem.seek(off)
                got = mem.readinto(view[:n])
            except OSError:
                got = 0
            if got != n:
                continue
            for i in range(0, n, 4096):
                d[off + i] = zlib.crc32(view[i:i + 4096])
    return d


def hot_pages(mem, pid, wait=8):
    d0 = digest(mem, pid)
    time.sleep(wait)
    d1 = digest(mem, pid)
    return [a for a in d0 if d1.get(a) is not None and d0[a] != d1[a]]


def scan_pairs(mem, pages):
    """Adjacent (x,z) double pairs in the given pages decoding in-grid."""
    out = {}
    for a in pages:
        try:
            mem.seek(a)
            buf = mem.read(4096)
        except OSError:
            continue
        for i in range(0, 4096 - 16, 8):
            z, = struct.unpack_from('<d', buf, i + 8)
            if not (LAT_MIN * DEG < z < LAT_MAX * DEG):
                continue
            x, = struct.unpack_from('<d', buf, i)
            lon_w = -x / DEG
            if LON_MIN < lon_w < LON_MAX:
                out[a + i] = (z / DEG, lon_w)
    return out


def find_boat(mem, pid, grid, cached=None, log=print):
    """LEGACY / DIAGNOSTIC ONLY.

    Locates position pairs whose transform output matches the game's grid
    STRING - but the string is computed from a wrongly-projected position
    and transiently holds other entities' grids, so matches are not proof
    of the boat.  Use read_boat() instead.
    """
    hot = hot_pages(mem, pid)
    log(f"  {len(hot)} hot pages")
    if not hot:
        return None
    s0 = scan_pairs(mem, hot)
    time.sleep(4)
    s1 = scan_pairs(mem, hot)
    match0 = {a: p for a, p in s0.items() if compute_grid(*p) == grid}
    match1 = {a: p for a, p in s1.items() if compute_grid(*p) == grid}
    moving = sorted(a for a in match0 if a in match1 and match0[a] != match1[a])
    if moving:
        return moving[0]
    both = sorted(set(match0) & set(match1))
    if len(both) == 1:
        return both[0]
    if both:
        if cached:
            return min(both, key=lambda a: abs(a - cached))
        log(f"  ambiguous: {len(both)} static matches, picking first")
        return both[0]
    return None


def main():
    if len(sys.argv) == 3:
        print(compute_grid(float(sys.argv[1]), float(sys.argv[2])))
        return
    watch = '--watch' in sys.argv
    try:
        os.nice(19)
    except OSError:
        pass
    pid = find_pid()
    mem = open(f'/proc/{pid}/mem', 'rb', buffering=0)

    pos = read_boat(mem)
    if pos:
        lat, lon_w = pos
        print(f"boat (true/map) position: {lat:.4f}N {lon_w:.4f}W "
              f"-> {compute_grid(lat, lon_w)}")
    else:
        print("boat position: not available (menu / chain broken)")
    print(f"game grid string (wrong calc): {read_grid(mem)}")
    if not watch:
        return

    last = None
    while True:
        time.sleep(1)
        pos = read_boat(mem)
        if pos:
            lat, lon_w = pos
            line = (f"{time.strftime('%H:%M:%S')} true: {lat:8.4f}N "
                    f"{lon_w:8.4f}W -> {compute_grid(lat, lon_w)}")
        else:
            line = f"{time.strftime('%H:%M:%S')} true: ---"
        line += f"  |  string: {read_grid(mem)}"
        if line != last:
            print(line, flush=True)
            last = line


if __name__ == '__main__':
    main()