#!/usr/bin/env python3
"""High-frequency watcher for the live grid string candidates in sh3.exe static data.
Targets:
  0x53cfec  "BE 31"        (boat grid candidate)
  0x547268  "Grid BE 32..." (grid+coords widget)
  surrounding doubles/pointers
Polls every 0.25s, logs any byte change with timestamp. Bounded output.
"""
import os, struct, sys, time

PID = 2543
LOG = os.path.join(os.path.dirname(__file__), 'data', 'grid_string_watch.log')

# (label, addr, length)
TARGETS = [
    ("gridA",   0x53cfec, 16),   # "BE 31\0D\0..."
    ("gridA_d", 0x53cfb0, 0x38), # doubles block
    ("gridB",   0x547268, 48),   # "Grid BE 32\06' E, Lat  53..."
    ("gridB_p", 0x547260, 8),    # preceding ints
]

def read(mem, addr, ln):
    mem.seek(addr)
    return mem.read(ln)

def fmt(buf):
    return buf.hex()

def main():
    mem = open(f'/proc/{PID}/mem', 'rb', buffering=0)
    state = {}
    for label, addr, ln in TARGETS:
        state[label] = read(mem, addr, ln)
    with open(LOG, 'a') as f:
        f.write(f"=== watcher start {time.time():.0f} ===\n")
        f.write("".join(f"  {l} @0x{a:08x}: {state[l]!r}\n" for l, a, _ in TARGETS))
        f.flush()
    print(f"watching {len(TARGETS)} targets, logging to {LOG}", flush=True)
    last_flush = time.time()
    while True:
        time.sleep(0.25)
        ts = time.time()
        lines = []
        for label, addr, ln in TARGETS:
            try:
                cur = read(mem, addr, ln)
            except OSError:
                continue
            if cur != state[label]:
                lines.append(f"{ts:.2f} {label} @0x{addr:08x}: {fmt(state[label])} -> {fmt(cur)}")
                state[label] = cur
        if lines:
            with open(LOG, 'a') as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
            print("\n".join(lines), flush=True)

if __name__ == '__main__':
    main()