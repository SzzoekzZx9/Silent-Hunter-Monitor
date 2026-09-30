#!/usr/bin/env python3
"""
Snapshot / diff engine for SH3 memory (the "ruling out" tool).

Takes a full snapshot of every rw page (4KB) in the process, storing a
hash per page. Two snapshots can then be diffed to show exactly which
pages changed in between. Combined with doing something specific in-game
between snapshots (letting time pass, moving the boat, crossing a grid
boundary), this classifies memory:

  - pages that change between ANY two snapshots a few seconds apart
    -> per-frame sim/render state (not interesting for a stable pointer)
  - pages that change only when the boat crosses a grid boundary
    -> grid-tracking state (exactly what issue #17 needs)
  - pages that never change -> static/config, ruled out

Snapshot files are gzipped JSON: {page_addr_hex: sha1_hex}. A full pass
over ~3.4 GB takes on the order of 30-60 s.

Usage:
  sudo python3 snapdiff.py snap <pid> <out.snap>
  sudo python3 snapdiff.py snap 230142 before.snap

  python3 snapdiff.py diff <a.snap> <b.snap> [--min-run N]
      Pages whose hash changed / appeared / vanished, clustered into
      contiguous runs. --min-run N hides runs shorter than N pages.

  sudo python3 snapdiff.py watch <pid> <interval> [count]
      Repeatedly snapshots and diffs against the previous pass, printing
      per-pass changed-page runs. Good for spotting per-frame churn.
"""
import sys
import os
import json
import gzip
import hashlib
import time

PAGE = 4096


def iter_rw_regions(pid):
    with open(f'/proc/{pid}/maps') as mf:
        for line in mf:
            parts = line.split()
            if 'rw' not in parts[1]:
                continue
            s, e = parts[0].split('-')
            yield int(s, 16), int(e, 16)


def snapshot(pid, out_file):
    """Hash every rw page; write {addr: sha1} snapshot."""
    t0 = time.time()
    pages = {}
    total = 0
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for start, end in iter_rw_regions(pid):
            for addr in range(start, end, PAGE):
                try:
                    mem.seek(addr)
                    buf = mem.read(PAGE)
                except OSError:
                    continue
                if len(buf) < PAGE:
                    continue
                total += PAGE
                pages[f'{addr:x}'] = hashlib.sha1(buf).hexdigest()
    with gzip.open(out_file, 'wt') as f:
        json.dump(pages, f, separators=(',', ':'))
    dt = time.time() - t0
    print(f"snapshot: {len(pages)} pages ({total/1e6:.0f} MB) "
          f"in {dt:.1f}s -> {out_file}")


def load_snap(path):
    with gzip.open(path, 'rt') as f:
        return json.load(f)


def diff(a_file, b_file, min_run=1):
    a = load_snap(a_file)
    b = load_snap(b_file)
    changed = sorted(
        int(k, 16) for k in a
        if k in b and a[k] != b[k])
    gone = sorted(int(k, 16) for k in a if k not in b)
    new = sorted(int(k, 16) for k in b if k not in a)

    def runs(addrs):
        out = []
        for x in addrs:
            if out and x == out[-1][1] + PAGE:
                out[-1][1] = x
            else:
                out.append([x, x])
        return [r for r in out if (r[1] - r[0]) // PAGE + 1 >= min_run]

    print(f"a: {len(a)} pages, b: {len(b)} pages")
    print(f"changed: {len(changed)} pages in {len(runs(changed))} runs, "
          f"gone: {len(gone)}, new: {len(new)}")
    for name, addrs in (('CHANGED', changed), ('GONE', gone), ('NEW', new)):
        for s, e in runs(addrs)[:400]:
            n = (e - s) // PAGE + 1
            print(f"  {name:7} {s:08x}-{e:08x} ({n} pg)")


def watch(pid, interval, count):
    """Snapshot repeatedly; diff each pass against the previous one."""
    prev = None
    tmp = '/tmp/.snapdiff_watch.tmp'
    for i in range(count):
        snapshot(pid, tmp)
        if prev is not None:
            print(f"--- pass {i}: diff vs previous ---")
            diff(prev, tmp)
        # keep a copy as the previous snapshot
        prev = f'/tmp/.snapdiff_watch_{i}.snap'
        os.replace(tmp, prev)
        time.sleep(interval)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'snap':
        snapshot(int(sys.argv[2]), sys.argv[3])
    elif cmd == 'diff':
        min_run = 1
        if '--min-run' in sys.argv:
            min_run = int(sys.argv[sys.argv.index('--min-run') + 1])
        diff(sys.argv[2], sys.argv[3], min_run)
    elif cmd == 'watch':
        watch(int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()