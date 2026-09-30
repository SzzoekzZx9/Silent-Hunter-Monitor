#!/usr/bin/env python3
"""Value-level watcher for a specific set of pages (e.g. a crossing burst).

Reads the target pages every interval and logs every 4-byte word change:
    <wall_ts> <addr> <old> <new>        (hex)
Memory-safe: only the target page set is held (typically a few thousand pages).

Usage: python3 -u value_watch.py <pid> <pages_file> [interval_seconds]
"""
import struct
import sys
import time

PID = int(sys.argv[1])
PAGES_FILE = sys.argv[2]
INTERVAL = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
LOG = '/tmp/value_watch.log'

pages = sorted(int(l, 16) for l in open(PAGES_FILE) if l.strip())
print(f"watching {len(pages)} pages ({len(pages)*4} KB) every {INTERVAL}s -> {LOG}")


def read_all(mem):
    out = {}
    for p in pages:
        try:
            mem.seek(p)
            out[p] = mem.read(4096)
        except OSError:
            continue
    return out


def main():
    mem = open(f'/proc/{PID}/mem', 'rb', buffering=0)
    prev = read_all(mem)
    log = open(LOG, 'a', buffering=1)
    while True:
        time.sleep(INTERVAL)
        cur = read_all(mem)
        ts = time.time()
        n = 0
        for p, buf in cur.items():
            old = prev.get(p)
            if old is None or old == buf:
                continue
            for off in range(0, 4096, 4):
                if buf[off:off+4] != old[off:off+4]:
                    log.write(f"{ts:.1f} {p+off:x} "
                              f"{old[off:off+4].hex()} {buf[off:off+4].hex()}\n")
                    n += 1
        prev = cur
        if n:
            print(f"{ts:.0f}: {n} word changes")


if __name__ == '__main__':
    main()