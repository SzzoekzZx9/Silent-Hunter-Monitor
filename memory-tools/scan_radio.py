#!/usr/bin/env python3
"""
Radio message memory scanner for Silent Hunter 3.

Scans the running SH3 process for the "Radio Message Received" signature and
prints the raw log strings found at each match, WITHOUT the format validation
that the production log collector applies (which is scoped to ship/aircraft
spotted messages). This lets us see exactly what radio message logs look like
in memory so we can wire up the message_received collector correctly.

Usage:
  sudo python3 memory-tools/scan_radio.py [signature]
      Scan for a signature (default 'Radio Message Received') and print the
      raw string at each match.

  sudo python3 memory-tools/scan_radio.py dump <hex_addr> <size>
      Dump a memory region as null-separated printable strings.
      Example: scan_radio.py dump 0xacc92100 0x400
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
import utils

DEFAULT_SIG = 'Radio Message Received'
SCAN_START = 0x08048000
SCAN_END = 0xC0000000


def scan(sig_text):
    # Interpret escape sequences (e.g. \n, \t) so newlines can be passed on the CLI
    sig_text = sig_text.encode('utf-8', errors='backslashreplace').decode('unicode_escape')
    sig = sig_text.encode('utf-8')
    pid = utils.get_pid_by_name()
    if not pid:
        print(f"Could not find SH3 process ('{utils.PROCESS_NAME}')")
        sys.exit(1)
    print(f"PID: {pid}")
    print(f"Scanning 0x{SCAN_START:08x}-0x{SCAN_END:08x} for: {sig_text!r}")
    print("-" * 70)

    seeds = []
    current_page = SCAN_START
    total_pages = (SCAN_END - SCAN_START) // 4096
    pages_processed = 0
    while current_page < SCAN_END:
        page_data = utils.read_memory_chunk(pid, current_page, 4096)
        if page_data and len(page_data) >= len(sig):
            offset = 0
            while True:
                idx = page_data.find(sig, offset)
                if idx == -1:
                    break
                seeds.append(current_page + idx)
                offset = idx + 1
        current_page += 4096
        pages_processed += 1
        if pages_processed % 50000 == 0:
            print(f"  ...{pages_processed}/{total_pages} pages, {len(seeds)} seeds so far")

    print(f"\nFound {len(seeds)} seed addresses for {sig_text!r}")
    print("-" * 70)
    for s in seeds[:200]:
        text = utils.read_log_string(pid, s)
        print(f"  0x{s:08x}: {text!r}")


def dump(addr, size):
    pid = utils.get_pid_by_name()
    if not pid:
        print(f"Could not find SH3 process ('{utils.PROCESS_NAME}')")
        sys.exit(1)
    print(f"PID: {pid}")
    print(f"===== DUMP 0x{addr:08x} (+{size:#x}) =====")
    data = utils.read_memory_chunk(pid, addr, size)
    # Print as null-separated strings, with their offset
    pos = 0
    for chunk in data.split(b'\x00'):
        if len(chunk) > 2:
            try:
                print(f"  +0x{pos:04x}: {chunk.decode('utf-8', errors='replace')!r}")
            except Exception:
                pass
        pos += len(chunk) + 1


def discover(sig_text):
    """Run the full LogCollector discovery path (find_all_seeds + window scan)
    with the radio message validator, to verify the production collector logic."""
    sig = sig_text.encode('utf-8')
    pid = utils.get_pid_by_name()
    if not pid:
        print(f"Could not find SH3 process ('{utils.PROCESS_NAME}')")
        sys.exit(1)
    print(f"PID: {pid}")
    print(f"Discovering log array for signature {sig_text!r} with radio validator")
    print("-" * 70)
    import time
    t0 = time.time()
    addrs, logs = utils.discover_log_array(
        pid, sig, SCAN_START, SCAN_END, 0x1000,
        validator=utils.validate_radio_message_format
    )
    elapsed = time.time() - t0
    print(f"\nDiscovery took {elapsed:.1f}s")
    print(f"Found {len(logs)} valid radio messages at {len(addrs)} addresses:")
    print("-" * 70)
    for addr, text in logs:
        print(f"  0x{addr:08x}: {text!r}")


def hexdump(addr, size):
    """Dump a memory region as hex + ASCII."""
    pid = utils.get_pid_by_name()
    if not pid:
        print(f"Could not find SH3 process ('{utils.PROCESS_NAME}')")
        sys.exit(1)
    print(f"PID: {pid}")
    print(f"===== HEXDUMP 0x{addr:08x} (+{size:#x}) =====")
    data = utils.read_memory_chunk(pid, addr, size)
    import struct
    for i in range(0, len(data), 16):
        hexpart = ' '.join(f'{b:02x}' for b in data[i:i+16])
        asciipart = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
        # Also try interpreting 4-byte words as floats
        floats = ''
        for j in range(0, min(16, len(data) - i), 4):
            if i + j + 4 <= len(data):
                f = struct.unpack('<f', data[i+j:i+j+4])[0]
                if f == f and -10000 <= f <= 10000 and f != 0:
                    floats += f' f{i+j}={f:.4f}'
        print(f'  0x{addr+i:08x}: {hexpart:<48}  {asciipart}{floats}')


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == 'hexdump':
        if len(sys.argv) != 4:
            print("Usage: scan_radio.py hexdump <hex_addr> <size>")
            sys.exit(1)
        hexdump(int(sys.argv[2], 16), int(sys.argv[3], 16))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'discover':
        sig_text = sys.argv[2] if len(sys.argv) > 2 else 'From:'
        discover(sig_text)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'dump':
        if len(sys.argv) != 4:
            print("Usage: scan_radio.py dump <hex_addr> <size>")
            sys.exit(1)
        dump(int(sys.argv[2], 16), int(sys.argv[3], 16))
    else:
        sig_text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SIG
        scan(sig_text)


if __name__ == "__main__":
    main()