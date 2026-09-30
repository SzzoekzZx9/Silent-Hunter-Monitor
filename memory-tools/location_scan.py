#!/usr/bin/env python3
"""
Location memory scanner for Silent Hunter 3.

Helps discover the memory addresses that hold the uboat's latitude and
longitude. SH3 stores coordinates as 32-bit floats. The workflow is the
classic "scan -> narrow" approach used by tools like Cheat Engine:

  1. In game, note your current latitude (e.g. 54.3). Run a scan for that
     value with a small tolerance. This produces a dump of candidate
     addresses.
  2. Move your boat so the latitude changes. Note the new latitude.
  3. Run a "narrow" pass against the previous dump, keeping only addresses
     whose current value is close to the new latitude. Repeat until only a
     handful of addresses remain.
  4. Do the same for longitude.
  5. Once you have a stable address, figure out whether it is a direct
     address or a pointer+offset (compare with collectors/speed.py and
     collectors/depth.py which use a pointer + offset). Update
     collectors/location.py with LAT_PTR/LAT_OFFSET and LON_PTR/LON_OFFSET.

Usage:
  # First scan: find candidates for a known value
  sudo python3 location_scan.py scan <pid> <value> <tolerance> <output_file>
  sudo python3 location_scan.py scan $(pgrep -f sh3.exe) 54.3 0.5 lat_dump1.txt

  # Narrow: filter a previous dump against a new known value
  sudo python3 location_scan.py narrow <pid> <value> <tolerance> <input_file> <output_file>
  sudo python3 location_scan.py narrow $(pgrep -f sh3.exe) 55.1 0.5 lat_dump1.txt lat_dump2.txt

  # Intersect two dumps (keep addresses present in both, report value changes)
  python3 location_scan.py intersect <file1> <file2>

Only scans rw memory regions. Reads /proc/<pid>/mem, so it must run as root.
"""
import sys
import os
import struct


def _iter_rw_regions(pid):
    """Yield (start, end) for each read-write mapped region of pid."""
    maps_file = f'/proc/{pid}/maps'
    with open(maps_file, 'r') as mf:
        for line in mf:
            parts = line.split()
            if len(parts) < 2:
                continue
            perm = parts[1]
            if 'rw' not in perm:
                continue
            start_str, end_str = parts[0].split('-')
            yield int(start_str, 16), int(end_str, 16)


def scan_memory(pid, target_val, tolerance, out_file):
    """Scan all rw memory for floats within tolerance of target_val."""
    mem_file = f'/proc/{pid}/mem'
    print(f"Scanning for {target_val} (+/- {tolerance})...")
    found_count = 0

    with open(mem_file, 'rb') as f, open(out_file, 'w') as f_out:
        for start_addr, end_addr in _iter_rw_regions(pid):
            try:
                f.seek(start_addr)
                region_size = end_addr - start_addr
                chunk_size = 1024 * 1024
                current_pos = start_addr
                bytes_read = 0

                while bytes_read < region_size:
                    read_len = min(chunk_size, region_size - bytes_read)
                    chunk = f.read(read_len)
                    if not chunk:
                        break

                    for i in range(0, len(chunk) - 3, 4):
                        try:
                            val = struct.unpack_from('f', chunk, i)[0]
                        except struct.error:
                            continue
                        if val != val:  # NaN
                            continue
                        if abs(val - target_val) <= tolerance:
                            abs_addr = current_pos + i
                            f_out.write(f"0x{abs_addr:08x} {val:.6f}\n")
                            found_count += 1

                    bytes_read += read_len
                    current_pos += read_len
            except (OSError, IOError):
                continue

    return found_count


def _load_addresses(filename):
    """Load a dump file into {address: value}."""
    addrs = {}
    with open(filename, 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 2:
                addr = int(parts[0], 16)
                val = float(parts[1])
                addrs[addr] = val
    return addrs


def narrow_memory(pid, target_val, tolerance, in_file, out_file):
    """Re-read each address in in_file and keep those close to target_val."""
    mem_file = f'/proc/{pid}/mem'
    candidates = _load_addresses(in_file)
    print(f"Narrowing {len(candidates)} candidates against {target_val} (+/- {tolerance})...")
    kept = 0

    with open(mem_file, 'rb') as f, open(out_file, 'w') as f_out:
        for addr in sorted(candidates):
            try:
                f.seek(addr)
                data = f.read(4)
                if len(data) < 4:
                    continue
                val = struct.unpack('<f', data)[0]
                if val != val:  # NaN
                    continue
                if abs(val - target_val) <= tolerance:
                    f_out.write(f"0x{addr:08x} {val:.6f}\n")
                    kept += 1
            except (OSError, IOError):
                continue

    return kept


def intersect_files(file1, file2):
    """Report addresses present in both dumps and how the value changed."""
    data1 = _load_addresses(file1)
    data2 = _load_addresses(file2)
    common = set(data1.keys()) & set(data2.keys())
    print(f"File 1 entries: {len(data1)}")
    print(f"File 2 entries: {len(data2)}")
    print(f"Common addresses: {len(common)}")
    print("\n--- CHANGED VALUES (candidates that moved) ---")
    shown = 0
    for addr in sorted(common):
        v1, v2 = data1[addr], data2[addr]
        if abs(v1 - v2) > 0.001:
            print(f"0x{addr:08x}  {v1:.6f} -> {v2:.6f}  (delta {v2 - v1:+.6f})")
            shown += 1
    if shown == 0:
        print("(no values changed between dumps)")
    print(f"\n{shown} changed, {len(common) - shown} unchanged")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'scan':
        if len(sys.argv) != 6:
            print("Usage: location_scan.py scan <pid> <value> <tolerance> <output_file>")
            sys.exit(1)
        pid = int(sys.argv[2])
        target_val = float(sys.argv[3])
        tolerance = float(sys.argv[4])
        out_file = sys.argv[5]
        count = scan_memory(pid, target_val, tolerance, out_file)
        print(f"Done. {count} candidates saved to {out_file}")

    elif cmd == 'narrow':
        if len(sys.argv) != 7:
            print("Usage: location_scan.py narrow <pid> <value> <tolerance> <input_file> <output_file>")
            sys.exit(1)
        pid = int(sys.argv[2])
        target_val = float(sys.argv[3])
        tolerance = float(sys.argv[4])
        in_file = sys.argv[5]
        out_file = sys.argv[6]
        kept = narrow_memory(pid, target_val, tolerance, in_file, out_file)
        print(f"Done. {kept} candidates kept in {out_file}")

    elif cmd == 'intersect':
        if len(sys.argv) != 4:
            print("Usage: location_scan.py intersect <file1> <file2>")
            sys.exit(1)
        intersect_files(sys.argv[2], sys.argv[3])

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()