#!/usr/bin/env python3
"""Targeted poller for crew Fatigue/Morale value entries (story #12).

Discovers all property entries whose key is 'Fatigue' or 'Morale', resolves
each to (crewman name, value address), then polls just those addresses.
Bounded by design: ~150 addresses x 64 bytes, polled every 2s.

Usage: nohup python3 crew_poll.py <pid> > /tmp/crew_poll.out 2>&1 &
Logs value changes to /tmp/crew_poll.log
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

LOG = '/tmp/crew_poll.log'
ENTRY_RE = re.compile(rb'\x04\x00....(\x75\x80|\x46\x81)', re.S)
POLL_KEYS = ('Fatigue', 'Morale')


def read_entries_around(pid, addr, back=0x1500, fwd=0x60):
    data = utils.read_memory_chunk(pid, addr - back, back + fwd)
    out = []
    for m in ENTRY_RE.finditer(data):
        hdr = m.start()
        end = data.find(b'\x00', hdr + 8)
        if end == -1 or end - hdr - 8 > 48:
            continue
        val = data[hdr + 8:end]
        if val and all(0x20 <= b < 0x7f for b in val):
            out.append((addr - back + hdr, val.decode('ascii')))
    return out


def discover(pid):
    """Return list of (name, key, value_addr, current_value)."""
    watches = []
    hits = utils.fast_seed_scan(pid, 0x08048000, 0xC0000000, b'Fatigue')
    hits += utils.fast_seed_scan(pid, 0x08048000, 0xC0000000, b'Morale')
    for h in hits:
        entries = read_entries_around(pid, h)
        # locate the key entry at h
        idx = None
        for i, (a, v) in enumerate(entries):
            if a <= h < a + 0x30 and v in POLL_KEYS:
                idx = i
                break
        if idx is None or idx + 1 >= len(entries):
            continue
        key = entries[idx][1]
        val_addr, val = entries[idx + 1]
        # nearest Name->value before this key
        name = '?'
        for i in range(idx - 1, -1, -1):
            if entries[i][1] == 'Name' and i + 1 < len(entries):
                name = entries[i + 1][1]
                break
        watches.append((name, key, val_addr, val))
    return watches


def main():
    pid = int(sys.argv[1])
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    watches = discover(pid)
    print(f'polling {len(watches)} entries every {interval}s')
    log = open(LOG, 'a', buffering=1)
    log.write(f"=== crew_poll start {time.strftime('%H:%M:%S')} "
              f"pid={pid} watches={len(watches)} ===\n")
    for name, key, addr, val in watches:
        log.write(f"{time.strftime('%H:%M:%S')} INIT {name} {key} "
                  f"@0x{addr:08x} = {val}\n")
    mem = open(f'/proc/{pid}/mem', 'rb', buffering=0)
    while True:
        time.sleep(interval)
        ts = time.strftime('%H:%M:%S')
        for name, key, addr, last in watches:
            try:
                mem.seek(addr)
                raw = mem.read(32)
            except (IOError, OSError):
                continue
            end = raw.find(b'\x00')
            val = raw[:end].decode('ascii', errors='replace') if end else ''
            if val != last:
                log.write(f'{ts} CHANGE {name} {key} @0x{addr:08x} '
                          f'{last!r} -> {val!r}\n')
                # update in place
                for i, w in enumerate(watches):
                    if w[2] == addr:
                        watches[i] = (name, key, addr, val)
                        break


if __name__ == '__main__':
    main()