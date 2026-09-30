#!/usr/bin/env python3
"""Shadow watcher for grid-crossing capture (issue #17).

Keeps a sparse on-disk shadow copy of all rw game memory below 0x80000000.
Every interval:
  1. digest all pages (RAM: addr -> sha1, ~150MB) and log which changed
     -> shadow_intervals.log  (t0 t1 n addr addr ...)
  2. for each changed page, diff its content against the shadow copy.
     If the page had been QUIET (unchanged) for >= QUIET_MIN intervals,
     log every 4-byte value change  -> shadow_values.log
     (t addr old new  per word, hex). Quiet-page changes are the signal:
     a grid ID that sits stable for hours and flips at a crossing.
  3. update the shadow for changed pages.
Also samples boat heading/speed (via static ptr 0x554698) every 0.5s
  -> shadow_samples.csv

Memory-safe: only the digest dict (~150MB) is held in RAM. The shadow lives
on disk (sparse file). Logs are flushed line-by-line to the repo data dir.

Usage: python3 -u shadow_watch.py <pid> [interval_seconds]
Stop with SIGINT/SIGTERM (clean shutdown) or kill.
"""
import hashlib
import os
import signal
import struct
import sys
import time

PID = int(sys.argv[1])
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0

SPEED_PTR = 0x554698
ARENA_END = 0x80000000
PAGE = 4096
QUIET_MIN = 20         # intervals a page must be unchanged before its changes get value-logged (20*45s = 15 min)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
SHADOW = os.path.join(DATA, 'shadow.bin')
INTLOG = os.path.join(DATA, 'shadow_intervals.log')
VALLOG = os.path.join(DATA, 'shadow_values.log')
CSV = os.path.join(DATA, 'shadow_samples.csv')

run = True


def stop(*a):
    global run
    run = False


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def rw_pages():
    pages = []
    with open(f'/proc/{PID}/maps') as mf:
        for line in mf:
            p = line.split()
            if 'rw' in p[1]:
                s, e = p[0].split('-')
                s, e = int(s, 16), int(e, 16)
                if e <= ARENA_END:
                    pages.extend(range(s, e, PAGE))
    return pages


def main():
    os.makedirs(DATA, exist_ok=True)
    pages = rw_pages()
    print(f"pid={PID} arena {len(pages)} pages ({len(pages)*PAGE//1024//1024} MB), "
          f"interval {INTERVAL}s, quiet_min {QUIET_MIN}", flush=True)

    mem = open(f'/proc/{PID}/mem', 'rb', buffering=0)
    shadow = os.open(SHADOW, os.O_RDWR | os.O_CREAT, 0o600)

    # initial snapshot: fill shadow + digests
    digests = {}
    t0 = time.time()
    for addr in pages:
        try:
            mem.seek(addr)
            buf = mem.read(PAGE)
        except OSError:
            continue
        digests[addr] = hashlib.sha1(buf).digest()
        os.pwrite(shadow, buf, addr)
    print(f"initial snapshot: {len(digests)} pages in {time.time()-t0:.0f}s", flush=True)

    # last interval index each page changed (for quiet-age); -1 = never
    last_change = {}
    interval_no = 0

    intlog = open(INTLOG, 'a', buffering=1)
    vallog = open(VALLOG, 'a', buffering=1)
    csv = open(CSV, 'a', buffering=1)

    def sample():
        try:
            mem.seek(SPEED_PTR)
            base = struct.unpack('<I', mem.read(4))[0]
            mem.seek(base + 0x64)
            hdg, spd = struct.unpack('<2f', mem.read(8))
            return hdg, spd
        except (OSError, struct.error):
            return None

    next_digest = time.time() + INTERVAL
    while run:
        now = time.time()
        s = sample()
        if s is not None:
            csv.write(f"{now:.1f},{s[0]:.2f},{s[1]:.2f}\n")
        if now >= next_digest:
            interval_no += 1
            changed = []
            for addr in pages:
                try:
                    mem.seek(addr)
                    buf = mem.read(PAGE)
                except OSError:
                    continue
                d = hashlib.sha1(buf).digest()
                if digests.get(addr) != d:
                    changed.append((addr, buf, d))
            ts = time.time()
            intlog.write(f"{now:.1f} {ts:.1f} {len(changed)} " +
                         " ".join(f"{a:x}" for a, _, _ in changed) + "\n")
            nval = 0
            for addr, buf, d in changed:
                lc = last_change.get(addr, -1)
                quiet = (interval_no - 1 - lc) >= QUIET_MIN if lc >= 0 else True
                if quiet:
                    old = os.pread(shadow, PAGE, addr)
                    n = min(len(old), len(buf))
                    for off in range(0, n, 4):
                        o = old[off:off+4]
                        w = buf[off:off+4]
                        if o != w:
                            vallog.write(f"{ts:.1f} {addr+off:x} "
                                         f"{o.hex()} {w.hex()}\n")
                            nval += 1
                os.pwrite(shadow, buf, addr)
                digests[addr] = d
                last_change[addr] = interval_no
            print(f"interval #{interval_no}: {len(changed)} pages changed, "
                  f"{nval} quiet-value changes", flush=True)
            next_digest = ts + INTERVAL
            try:
                pages = rw_pages()
            except OSError:
                print("maps read failed; exiting", flush=True)
                break
        time.sleep(0.5)

    intlog.close(); vallog.close(); csv.close()
    os.close(shadow); mem.close()
    print("stopped cleanly", flush=True)


if __name__ == '__main__':
    main()