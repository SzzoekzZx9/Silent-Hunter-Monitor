import sys
import struct
import os
import subprocess

def scan_memory(pid, target_val, tolerance, out_file):
    mem_file = f'/proc/{pid}/mem'
    maps_file = f'/proc/{pid}/maps'

    print(f"Scanning for {target_val} (+/- {tolerance})...")

    found_count = 0

    with open(maps_file, 'r') as mf:
        with open(mem_file, 'rb') as f:
            for line in mf:
                parts = line.split()
                perm = parts[1]
                
                if 'rw' not in perm: 
                    continue

                addr_range = parts[0]
                start_str, end_str = addr_range.split('-')
                start_addr = int(start_str, 16)
                end_addr = int(end_str, 16)

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
                                
                                if abs(val - target_val) <= tolerance:
                                    abs_addr = current_pos + i
                                    # FIX: Added '0x' prefix for compatibility
                                    f_out.write(f"0x{abs_addr:08x} {val:.2f}\n")
                                    found_count += 1
                                    
                            except:
                                pass
                        
                        bytes_read += read_len
                        current_pos += read_len

                except (OSError, IOError):
                    continue
    
    return found_count

if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: python smart_dump.py <pid> <value> <tolerance> <output_file>")
        print("Example: python smart_dump.py 1234 71 5 depth_dump1.txt")
        sys.exit(1)

    pid = int(sys.argv[1])
    target_val = float(sys.argv[2])
    tolerance = float(sys.argv[3])
    out_filename = sys.argv[4]

    with open(out_filename, 'w') as f_out:
        count = scan_memory(pid, target_val, tolerance, f_out)

    print(f"Done. Results saved to {out_filename}")
    print(f"Total candidates found: {count}")
