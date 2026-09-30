#!/usr/bin/env python3
"""
Annotated memory map for the SH3 process.

Groups the (many) mappings from /proc/<pid>/maps into contiguous blocks,
labels them (game exe, game DLLs, wine runtime, heaps, stacks, known
landmarks from the monitor project) and prints a table sorted by address.

The point of the map is to RULE OUT regions: anything that is wine
runtime, graphics driver shared memory, or file-backed read-only data
cannot hold live game state. What remains (the exe's static data, the
game's own heaps) is where scan/narrow effort should go.

Usage:
  python3 memmap.py [pid]            # annotated map, grouped
  python3 memmap.py [pid] --rw       # only rw regions (live-state candidates)
"""
import sys
import os

# Known landmarks discovered during monitor development.
# (start, end, label) — matched against region addresses.
LANDMARKS = [
    (0x00554698, 0x00554698 + 4, "SPEED_PTR (live float ptr, collectors/speed.py)"),
    (0x005546d0, 0x005546d0 + 4, "GRID_PTR (STALE grid string, issue #17)"),
    (0x02c1f0000, 0x02c220000, "grid-name tables (many grids adjacent)"),
    (0x03b3e0000, 0x03b470000, "date->grid journal records (linked list, issue #17)"),
]

PAGE = 4096


def parse_maps(pid):
    """Return list of dicts: start, end, perms, offset, path."""
    regions = []
    with open(f'/proc/{pid}/maps') as f:
        for line in f:
            parts = line.split(None, 5)
            if len(parts) < 2:
                continue
            rng, perms = parts[0], parts[1]
            offset = parts[2] if len(parts) > 2 else '0'
            path = parts[5].strip() if len(parts) > 5 else ''
            s, e = rng.split('-')
            regions.append({
                'start': int(s, 16), 'end': int(e, 16),
                'perms': perms, 'offset': offset, 'path': path,
            })
    return regions


def classify(path, perms):
    """Coarse label for a region, used for ruling out."""
    if path:
        base = os.path.basename(path)
        if 'sh3.exe' in path or base in (
                'd3d9.dll', 'Intro.dll', 'MessageNet.dll', 'MissionEngine.dll',
                'SimData.dll', 'FileManager.dll', 'movietex.act'):
            return 'GAME'
        if '/wine/' in path or 'proton' in path or 'steamrt' in path.lower():
            return 'WINE'
        if path.startswith('/usr/lib') or path.startswith('/lib'):
            return 'SYSTEM'
        if '/dev/' in path or 'nvidia' in path.lower() or 'dri' in path:
            return 'GPU'
        return 'FILE'
    if 'x' in perms:
        return 'ANON-X'   # jitted/mapped code
    return 'ANON'         # heap/stack scratch


def group_regions(regions):
    """Merge adjacent regions with the same classification and path."""
    groups = []
    for r in regions:
        if groups:
            g = groups[-1]
            if (r['start'] == g['end'] and r['path'] == g['path']
                    and classify(r['path'], r['perms']) == g['cls']):
                g['end'] = r['end']
                g['perms'] = g['perms'] if g['perms'] == r['perms'] else 'mixed'
                g['rw_bytes'] += r['end'] - r['start'] if 'rw' in r['perms'] else 0
                continue
        groups.append({
            'start': r['start'], 'end': r['end'],
            'perms': r['perms'], 'path': r['path'],
            'cls': classify(r['path'], r['perms']),
            'rw_bytes': r['end'] - r['start'] if 'rw' in r['perms'] else 0,
        })
    return groups


def landmarks_for(start, end):
    return [lbl for (ls, le, lbl) in LANDMARKS if start <= ls < end]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = [a for a in sys.argv[1:] if a.startswith('--')]
    pid = int(args[0]) if args else None
    if pid is None:
        # find sh3.exe like utils.get_pid_by_name
        for d in os.listdir('/proc'):
            if d.isdigit():
                try:
                    with open(f'/proc/{d}/cmdline') as f:
                        if 'sh3.exe' in f.read():
                            pid = int(d)
                            break
                except OSError:
                    continue
    if pid is None:
        print("sh3.exe process not found", file=sys.stderr)
        sys.exit(1)

    only_rw = '--rw' in flags
    regions = parse_maps(pid)
    groups = group_regions(regions)

    total = sum(g['end'] - g['start'] for g in groups)
    rw_total = sum(g['rw_bytes'] for g in groups)
    print(f"pid {pid}: {len(groups)} region groups, "
          f"{total/1e6:.0f} MB mapped, {rw_total/1e6:.0f} MB rw\n")

    hdr = f"{'start':>10} {'end':>10} {'size':>9} {'perms':<6} {'rw?':<4} {'class':<7} landmark / path"
    print(hdr)
    print('-' * len(hdr))
    for g in groups:
        size = g['end'] - g['start']
        if only_rw and g['rw_bytes'] == 0:
            continue
        marks = landmarks_for(g['start'], g['end'])
        mark_txt = (' << ' + '; '.join(marks)) if marks else ''
        path = g['path'] or '(anon)'
        print(f"{g['start']:10x} {g['end']:10x} {size/1024:8.0f}K "
              f"{g['perms']:<6} {'yes' if g['rw_bytes'] else 'no':<4} "
              f"{g['cls']:<7} {path}{mark_txt}")

    # Summary by class — the ruling-out overview.
    print('\n--- by class (rw bytes only) ---')
    by_cls = {}
    for g in groups:
        by_cls[g['cls']] = by_cls.get(g['cls'], 0) + g['rw_bytes']
    for cls, b in sorted(by_cls.items(), key=lambda kv: -kv[1]):
        print(f"{cls:<8} {b/1e6:9.0f} MB")


if __name__ == '__main__':
    main()