import gdb

class DiagnosticScanLogStructs(gdb.Command):
    def __init__(self):
        super(DiagnosticScanLogStructs, self).__init__("diagnostic_scan", gdb.COMMAND_DATA)

    def read_log_string(self, address):
        """Reads a string by grabbing a 256-byte chunk and finding the null terminator."""
        try:
            chunk = gdb.selected_inferior().read_memory(address, 256)
            data = bytes(chunk)
            try:
                null_index = data.index(b'\x00')
                raw_string = data[:null_index]
            except ValueError:
                raw_string = data
            
            text = raw_string.decode('utf-8', errors='replace')
            return text
            
        except gdb.MemoryError:
            return ""
        except Exception:
            return ""

    def check_header(self, address, sig_header):
        """Checks if address has expected header 2 bytes before."""
        try:
            header_check = gdb.selected_inferior().read_memory(address - 2, 2)
            has_header = bytes(header_check) == sig_header
            return has_header, bytes(header_check)
        except gdb.MemoryError:
            return False, b'??'

    def analyze_spacing_pattern(self, matches):
        """Analyzes spacing pattern between matches."""
        if len(matches) < 2:
            return []
        
        deltas = []
        for i in range(1, len(matches)):
            delta = matches[i] - matches[i-1]
            deltas.append(delta)
        
        return deltas

    def invoke(self, arg, from_tty):
        try:
            SCAN_START = 0x08048000
            SCAN_END   = 0xC0000000
            SIG_TEXT = b'Ship spotted, sir!'
            SIG_HEADER = b'\x75\x80'
            
            print("=" * 80)
            print("DIAGNOSTIC LOG SCANNER - No Validation")
            print("=" * 80)
            print(f"Scanning range: 0x{SCAN_START:x} - 0x{SCAN_END:x}")
            print(f"Searching for: '{SIG_TEXT.decode('utf-8')}'")
            print(f"Expected header: {SIG_HEADER.hex()}")
            print("=" * 80)
            
            all_matches = []
            current_page = SCAN_START
            
            # Simple global search - no validation at all
            print("\n=== GLOBAL SEARCH IN PROGRESS ===")
            
            while current_page < SCAN_END:
                try:
                    page_data = gdb.selected_inferior().read_memory(current_page, 4096)
                    page_bytes = bytes(page_data)
                    
                    idx = 0
                    while idx < len(page_bytes) - len(SIG_TEXT):
                        if page_bytes[idx : idx + len(SIG_TEXT)] == SIG_TEXT:
                            candidate_addr = current_page + idx
                            all_matches.append(candidate_addr)
                        
                        idx += 1

                except gdb.MemoryError:
                    pass
                
                current_page += 4096
                
                # Progress indicator every 256MB
                if (current_page - SCAN_START) % 0x10000000 == 0:
                    progress = ((current_page - SCAN_START) / (SCAN_END - SCAN_START)) * 100
                    print(f"  Progress: {progress:.1f}% ({len(all_matches)} matches so far)")
            
            print(f"\nSearch complete. Total matches found: {len(all_matches)}")
            print("=" * 80)
            
            if not all_matches:
                print("No matches found!")
                return
            
            # Analyze spacing pattern
            print("\n=== SPACING PATTERN ANALYSIS ===")
            deltas = self.analyze_spacing_pattern(all_matches)
            
            if deltas:
                # Find most common deltas
                from collections import Counter
                delta_counter = Counter(deltas)
                print("\nMost common spacings between matches:")
                for delta, count in delta_counter.most_common(10):
                    print(f"  0x{delta:x} ({delta} bytes): {count} occurrences")
            
            # Detailed analysis of each match
            print("\n=== DETAILED MATCH ANALYSIS ===")
            print(f"\n{'Address':<16} {'Has Header':<10} {'Actual Header':<12} {'Log Text'}")
            print("-" * 80)
            
            with_header_count = 0
            without_header_count = 0
            
            for addr in all_matches:
                has_header, actual_header = self.check_header(addr, SIG_HEADER)
                text = self.read_log_string(addr)
                
                header_status = "YES" if has_header else "NO"
                header_hex = actual_header.hex() if len(actual_header) == 2 else "??"
                
                if has_header:
                    with_header_count += 1
                else:
                    without_header_count += 1
                
                # Truncate text for display if too long
                display_text = text[:50] if len(text) > 50 else text
                
                print(f"0x{addr:<14x} {header_status:<10} {header_hex:<12} {display_text}")
            
            # Summary statistics
            print("\n" + "=" * 80)
            print("SUMMARY")
            print("=" * 80)
            print(f"Total matches: {len(all_matches)}")
            print(f"Matches WITH expected header (\\x75\\x80): {with_header_count}")
            print(f"Matches WITHOUT expected header: {without_header_count}")
            
            if with_header_count > 0:
                print(f"\nPercentage with correct header: {(with_header_count/len(all_matches))*100:.1f}%")
            
            # Show first and last addresses
            print(f"\nFirst match at: 0x{all_matches[0]:x}")
            print(f"Last match at: 0x{all_matches[-1]:x}")
            print(f"Address range: 0x{all_matches[-1] - all_matches[0]:x} ({(all_matches[-1] - all_matches[0]) // 1024 // 1024}MB)")
            
            # Look for bearing 340 specifically
            print("\n=== SEARCHING FOR BEARING 340 ===")
            found_bearing_340 = False
            for addr in all_matches:
                text = self.read_log_string(addr)
                if "340" in text:
                    has_header, actual_header = self.check_header(addr, SIG_HEADER)
                    print(f"\nFOUND BEARING 340 at 0x{addr:x}:")
                    print(f"  Text: {text}")
                    print(f"  Has header \\x75\\x80: {has_header}")
                    print(f"  Actual header: {actual_header.hex()}")
                    
                    # Show neighbors
                    print(f"  Neighbors (stride 0x40):")
                    for offset in [-0x80, -0x40, 0x40, 0x80]:
                        neighbor = addr + offset
                        n_text = self.read_log_string(neighbor)
                        n_has_header, n_header = self.check_header(neighbor, SIG_HEADER)
                        print(f"    0x{neighbor:x} ({offset:+x}): Header={n_has_header}, Text={n_text[:40]}")
                    
                    found_bearing_340 = True
            
            if not found_bearing_340:
                print("Bearing 340 log not found in any match!")
                print("\nSearching for any '340' in text:")
                current_page = SCAN_START
                while current_page < SCAN_END:
                    try:
                        page_data = gdb.selected_inferior().read_memory(current_page, 4096)
                        page_bytes = bytes(page_data)
                        
                        if b'340' in page_bytes:
                            idx = page_bytes.index(b'340')
                            addr_with_340 = current_page + idx
                            # Read surrounding context
                            context = gdb.selected_inferior().read_memory(addr_with_340 - 20, 60)
                            context_str = bytes(context).decode('utf-8', errors='replace')
                            print(f"\nFound '340' at 0x{addr_with_340:x}: ...{context_str}...")
                            if b'340' in page_bytes[idx+3:]:  # Look for second occurrence
                                idx2 = page_bytes.index(b'340', idx + 3)
                                addr_with_340_2 = current_page + idx2
                                context2 = gdb.selected_inferior().read_memory(addr_with_340_2 - 20, 60)
                                context_str2 = bytes(context2).decode('utf-8', errors='replace')
                                print(f"Found '340' at 0x{addr_with_340_2:x}: ...{context_str2}...")
                                break  # Only show first page
                    except gdb.MemoryError:
                        pass
                    current_page += 4096
            
            print("\n" + "=" * 80)
            print("Diagnostic complete!")
            print("=" * 80)
                
        except Exception as e:
            print(f"\nCritical Error: {e}")
            import traceback
            traceback.print_exc()

DiagnosticScanLogStructs()
