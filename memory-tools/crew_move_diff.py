#!/usr/bin/env python3
"""Targeted crew-location snapshot & diff toolkit (story #12 follow-up: stale nrc).

Problem being investigated: the roster `nrc` field (compartment) is live for
direct drags and game-initiated moves (dive/surface), but goes STALE for a
crewman displaced by a swap-drag (e.g. dragged-off sonar/radio staff keep
nrc=1 while sitting in a bunk). The game clearly knows the real location
(it renders them in the bunk and gives fatigue recovery), so a live field
must exist somewhere. This tool snapshots every place a crewman's name
appears in memory (any live bunk/assignment structure that references crew
by name will be inside one of these windows) plus the roster table, the
slot-node array, and all NrComp property entries — then diffs two snapshots
around a controlled in-game move.

Snapshot contents (pickle, gzip):
  roster_raw   : full 140-entry roster table bytes
  slots_raw    : full 34-node slot array bytes
  nrcomp       : {name: [(value_str, addr), ...]} all copies
  windows      : {addr: bytes} 0x4000-byte window centered on every
                 heap occurrence of every roster crewman's name
  names        : {name: [hit addrs]} for reference

Usage:
  python3 crew_move_diff.py snap  /path/to/snap.pkl.gz
  python3 crew_move_diff.py diff  A.pkl.gz B.pkl.gz

Experiment protocol (one move per snapshot pair):
  1. snap A
  2. perform ONE controlled move in-game (e.g. one swap-drag), wait ~5s
  3. snap B
  4. diff A B
"""
import gzip
import os
import pickle
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
from collectors import crew as C

WINDOW = 0x4000  # bytes around each name hit (centered)
ENTRY_RE = re.compile(rb'\x04\x00....(\x75\x80|\x46\x81)', re.S)


def find_name_hits(pid, names):
    """All heap addresses where any roster name occurs (as exact cstring)."""
    hits = {}
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        regions = [(s, e) for s, e in utils.iter_rw_regions(pid)
                   if e - s <= 0x2000000]
        for start, end in regions:
            try:
                mem.seek(start)
                data = mem.read(end - start)
            except OSError:
                continue
            for name in names:
                needle = name.encode()
                off = 0
                while True:
                    i = data.find(needle, off)
                    if i < 0:
                        break
                    # require null terminator (exact string)
                    j = i + len(needle)
                    if j >= len(data) or data[j] == 0:
                        hits.setdefault(name, []).append(start + i)
                    off = i + 1
    return hits


def read_nrcomp(pid):
    """All NrComp property entries: {name: [(value, addr), ...]}."""
    out = {}

    def entries_around(mem, addr, back=0x1500, fwd=0x60):
        mem.seek(addr - back)
        data = mem.read(back + fwd)
        ents = []
        for m in ENTRY_RE.finditer(data):
            hdr = m.start()
            end = data.find(b'\x00', hdr + 8)
            if end == -1 or end - hdr - 8 > 48:
                continue
            val = data[hdr + 8:end]
            if val and all(0x20 <= b < 0x7f for b in val):
                ents.append((addr - back + hdr, val.decode('ascii')))
        return ents

    hits = utils.fast_seed_scan(pid, 0x08048000, 0xC0000000, b'NrComp')
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for h in hits:
            ents = entries_around(mem, h)
            idx = next((i for i, (a, v) in enumerate(ents)
                        if a <= h < a + 0x30 and v == 'NrComp'), None)
            if idx is None or idx + 1 >= len(ents):
                continue
            val_addr, val = ents[idx + 1]
            name = '?'
            for i in range(idx - 1, -1, -1):
                if ents[i][1] == 'Name' and i + 1 < len(ents):
                    name = ents[i + 1][1]
                    break
            if name != '?':
                out.setdefault(name, []).append((val, val_addr))
    return out


def snap(path):
    pid = utils.get_pid_by_name('sh3.exe')
    if not pid:
        sys.exit('sh3.exe not running')
    roster = C.parse_roster(C.read_roster(pid))
    names = [c['name'] for c in roster]
    print(f'{len(names)} live crew; scanning for name hits...')
    hits = find_name_hits(pid, names)
    n_hits = sum(len(v) for v in hits.values())
    print(f'{n_hits} name hits; reading {WINDOW:#x}-byte windows...')
    windows = {}
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for name, addrs in hits.items():
            for a in addrs:
                base = a - WINDOW // 2
                try:
                    mem.seek(base)
                    windows[a] = mem.read(WINDOW)
                except OSError:
                    pass
    data = {
        'roster_raw': C.read_roster(pid),
        'slots_raw': C.read_slots(pid),
        'nrcomp': read_nrcomp(pid),
        'windows': windows,
        'names': hits,
    }
    with gzip.open(path, 'wb') as f:
        pickle.dump(data, f, protocol=4)
    print(f'wrote {path} ({os.path.getsize(path) >> 20} MiB), '
          f'{len(windows)} windows')


def _roster_nrc_map(raw):
    out = {}
    for n in range(C.ROSTER_COUNT):
        e = raw[n * C.ROSTER_STRIDE:(n + 1) * C.ROSTER_STRIDE]
        name = C._valid_name(e[0:0x34])
        if not name:
            continue
        nrc = struct.unpack_from('<i', e, 0x38)[0]
        if nrc == -1:
            continue
        if name not in out:
            out[name] = nrc
    return out


def diff(path_a, path_b):
    with gzip.open(path_a, 'rb') as f:
        A = pickle.load(f)
    with gzip.open(path_b, 'rb') as f:
        B = pickle.load(f)

    print('=== ROSTER nrc changes ===')
    na, nb = _roster_nrc_map(A['roster_raw']), _roster_nrc_map(B['roster_raw'])
    for name in sorted(set(na) | set(nb)):
        if na.get(name) != nb.get(name):
            print(f'  {name:28s} nrc {na.get(name)} -> {nb.get(name)}')

    print('=== SLOT array changes ===')
    sa = C.parse_slots(A['slots_raw'])
    sb = C.parse_slots(B['slots_raw'])
    for name in sorted(set(sa) | set(sb)):
        if sa.get(name) != sb.get(name):
            print(f'  {name:28s} slot {sa.get(name)} -> {sb.get(name)}')

    print('=== NrComp changes ===')
    for name in sorted(set(A['nrcomp']) | set(B['nrcomp'])):
        va = sorted(A['nrcomp'].get(name, []))
        vb = sorted(B['nrcomp'].get(name, []))
        if va != vb:
            print(f'  {name:28s} {va} -> {vb}')

    print('=== Heap window changes (4-byte granularity) ===')
    common = set(A['windows']) & set(B['windows'])
    for addr in sorted(common):
        wa, wb = A['windows'][addr], B['windows'][addr]
        if wa == wb:
            continue
        i = 0
        changes = []
        while i < len(wa):
            if wa[i:i + 4] == wb[i:i + 4]:
                i += 4
                continue
            j = i
            while j < len(wa) and wa[j:j + 4] != wb[j:j + 4]:
                j += 4
            changes.append((i, j))
            i = j
        print(f'  window @0x{addr:08x}: {len(changes)} changed runs')
        for i, j in changes[:12]:
            lo = max(0, i - 8)
            print(f'    0x{addr - WINDOW // 2 + i:08x} (+{i - WINDOW // 2:+#x}): '
                  f'{wa[lo:j + 8].hex(" ")}'
                  f'  ->  {wb[lo:j + 8].hex(" ")}')
        if len(changes) > 12:
            print(f'    ... {len(changes) - 12} more runs')
    only_a = set(A['windows']) - set(B['windows'])
    only_b = set(B['windows']) - set(A['windows'])
    if only_a:
        print(f'  ({len(only_a)} windows only in A — heap moved; '
              f're-scan needed)')


if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] not in ('snap', 'diff'):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == 'snap':
        snap(sys.argv[2])
    else:
        diff(sys.argv[2], sys.argv[3])