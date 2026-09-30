#!/usr/bin/env python3
"""Continuous watcher for crew record regions (story #12 discovery).

Diffs the known crew-record memory regions every few seconds and logs every
change where the old or new bytes are printable text (crew values are stored
as text: names, '0.800000'-style floats, 'Yes'/'No').

Usage: nohup python3 crew_watch.py <pid> [interval_seconds] &
Logs to /tmp/crew_watch.log
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

LOG = '/tmp/crew_watch.log'

# Regions holding crew member records found via 'Eisenholtz'/'Linke-Stier'
# seed scans (2026-09-23). Each tuple: (start, end, label)
REGIONS = [
    (0x3b3eb400, 0x3b3ec400, 'hit1 career buf'),
    (0x3b4a3000, 0x3b4a6000, 'hit2 state buf'),
    (0x3e748400, 0x3e749400, 'hit3 career buf'),
    (0x61e48800, 0x61e49800, 'FR 0x61e buf'),
    (0xac4af000, 0xac4b2000, 'FR 0xac4b buf'),
    (0xb376b800, 0xb376d800, 'hit4 buf'),
]


def printable(b):
    return b and all(0x20 <= c < 0x7f for c in b)


def read_all(mem):
    out = {}
    for start, end, _label in REGIONS:
        try:
            mem.seek(start)
            out[start] = mem.read(end - start)
        except (IOError, OSError):
            out[start] = None
    return out


def main():
    pid = int(sys.argv[1])
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    mem = open(f'/proc/{pid}/mem', 'rb', buffering=0)
    prev = read_all(mem)
    log = open(LOG, 'a', buffering=1)
    log.write(f"=== crew_watch start {time.strftime('%H:%M:%S')} "
              f"pid={pid} interval={interval}s ===\n")
    print(f"watching {len(REGIONS)} regions every {interval}s -> {LOG}")
    while True:
        time.sleep(interval)
        cur = read_all(mem)
        ts = time.strftime('%H:%M:%S')
        for start, end, label in REGIONS:
            a, b = prev.get(start), cur.get(start)
            if a is None or b is None or a == b:
                continue
            # find changed 4-byte words; report runs of changes with context
            i = 0
            while i < len(a):
                if a[i:i+4] == b[i:i+4]:
                    i += 4
                    continue
                # collect the full changed run
                j = i
                while j < len(a) and a[j:j+4] != b[j:j+4]:
                    j += 4
                old, new = a[i:j], b[i:j]
                addr = start + i
                if printable(old) or printable(new):
                    log.write(f"{ts} [{label}] 0x{addr:08x} "
                              f"{old!r} -> {new!r}\n")
                else:
                    log.write(f"{ts} [{label}] 0x{addr:08x} "
                              f"{old.hex()} -> {new.hex()}\n")
                i = j
        prev = cur


if __name__ == '__main__':
    main()