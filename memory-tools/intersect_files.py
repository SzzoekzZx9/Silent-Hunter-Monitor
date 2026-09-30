import sys

def load_addresses(filename):
    addrs = {}
    print(f"Loading {filename}...")
    try:
        with open(filename, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    # FIX: Added base 16 to handle '0x...' prefix
                    addr = int(parts[0], 16) 
                    val = float(parts[1])
                    addrs[addr] = val
    except FileNotFoundError:
        print(f"File {filename} not found.")
        return {}
    except ValueError as e:
        print(f"Error parsing line: {line} - {e}")
        return {}
    return addrs

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 intersect_files.py <file1.txt> <file2.txt>")
        sys.exit(1)

    file1 = sys.argv[1]
    file2 = sys.argv[2]

    data1 = load_addresses(file1)
    data2 = load_addresses(file2)

    print(f"File 1 entries: {len(data1)}")
    print(f"File 2 entries: {len(data2)}")
    
    print("\n--- INTERSECTIONS (Addresses in both files) ---")
    
    # We look for addresses that are in BOTH
    common_addrs = set(data1.keys()) & set(data2.keys())
    
    found_count = 0
    
    # Open the output file for saving the results
    with open("intersect_dump.txt", 'w') as out_f:
        for addr in common_addrs:
            val1 = data1[addr]
            val2 = data2[addr]
            
            # Check if value changed significantly
            if abs(val1 - val2) > 0.1:
                # Pretty Print to Console
                print(f"Address: {hex(addr)} | Val1: {val1:.4f} -> Val2: {val2:.4f} (Delta: {val2-val1:+.4f})")
                
                # Save to file (Same format as smart dump)
                # We use the new value (val2) so this file acts like a fresh dump
                out_f.write(f"{hex(addr)} {val2:.2f}\n")
                
                found_count = True
            
    print(f"\nDone. Saved {found_count if found_count else 0} results to 'intersect_dump.txt'")
