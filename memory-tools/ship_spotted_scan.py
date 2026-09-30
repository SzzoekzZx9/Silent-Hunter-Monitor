import gdb

class ScanLogStructs(gdb.Command):
    def __init__(self):
        super(ScanLogStructs, self).__init__("scan_logs", gdb.COMMAND_DATA)

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

    def validate_log_format(self, text):
        """
        Validates that the log text matches expected format for ship spotted messages.
        
        Args:
            text: Log text to validate
        
        Returns:
            True if text is valid, False otherwise
        """
        # Check length - ship spotted logs should be reasonable length
        if len(text) > 100 or len(text) < 30:
            return False
        
        # Check that it starts with expected text
        if not text.startswith("Ship spotted, sir!"):
            return False
        
        # Check that it contains expected patterns
        # Should contain "Bearing XXX!" where XXX is a number
        if "Bearing" not in text or "!" not in text:
            return False
        
        # Check that it ends with expected range indicator
        # Should end with "Short range!", "Medium range!", or "Long range!"
        if not (text.endswith("Short range!") or 
                text.endswith("Medium range!") or 
                text.endswith("Long range!")):
            return False
        
        # Check for obvious garbage patterns (like other log entries embedded)
        if "[Log Entry" in text or "EntryText=" in text:
            return False
        
        return True

    def find_all_seeds(self, scan_start, scan_end, sig_text):
        """
        Finds all seed addresses in the memory range using signature only.
        
        Args:
            scan_start: Start address for scan
            scan_end: End address for scan
            sig_text: Signature text bytes
        
        Returns:
            List of seed addresses
        """
        seeds = []
        current_page = scan_start
        
        print(f"Searching for ALL seeds in range 0x{scan_start:x} - 0x{scan_end:x}...")
        
        while current_page < scan_end:
            try:
                page_data = gdb.selected_inferior().read_memory(current_page, 4096)
                page_bytes = bytes(page_data)
                
                idx = 0
                while idx < len(page_bytes) - len(sig_text):
                    if page_bytes[idx : idx + len(sig_text)] == sig_text:
                        candidate_addr = current_page + idx
                        
                        # Skip if already in our seed list (within reasonable proximity)
                        is_duplicate = False
                        for seed in seeds:
                            if abs(candidate_addr - seed) < 0x1000:  # Same page
                                is_duplicate = True
                                break
                        
                        if not is_duplicate:
                            seeds.append(candidate_addr)
                    
                    idx += 1

            except gdb.MemoryError:
                pass
            
            current_page += 4096
        
        return sorted(seeds)

    def comprehensive_window_scan(self, seed, window_size, sig_text):
        """
        Performs a comprehensive scan of a window around a seed address.
        Checks every address in the window for signature only.
        
        Args:
            seed: Seed address (center of window)
            window_size: Total window size to scan (half on each side)
            sig_text: Signature text bytes
        
        Returns:
            List of (address, text) tuples for matching logs
        """
        logs = []
        scan_start = seed - window_size
        scan_end = seed + window_size
        
        print(f"    Scanning window 0x{scan_start:x} - 0x{scan_end:x} around seed 0x{seed:x}...")
        
        # Check every potential address in the window
        current = scan_start
        
        while current < scan_end:
            try:
                # Check for signature ONLY (no header validation)
                check_bytes = gdb.selected_inferior().read_memory(current, len(sig_text))
                if bytes(check_bytes) == sig_text:
                    text = self.read_log_string(current)
                    # Validate format to filter out garbage like 0xb209724a
                    if self.validate_log_format(text):
                        logs.append((current, text))
            except gdb.MemoryError:
                pass
            
            current += 1  # Check every single byte position
        
        return logs

    def invoke(self, arg, from_tty):
        try:
            SCAN_START = 0x08048000
            SCAN_END   = 0xC0000000
            SIG_TEXT = b'Ship spotted, sir!'
            
            # Window size for comprehensive scan (2MB each side = 4MB total)
            WINDOW_SIZE = 0x200000
            
            print("=" * 60)
            print("SIGNATURE-ONLY LOG ARRAY SCANNER")
            print("=" * 60)
            print(f"Searching for: '{SIG_TEXT.decode('utf-8')}'")
            print(f"Validation: SIGNATURE TEXT ONLY (no header check)")
            print(f"Window size per seed: {WINDOW_SIZE // 1024 // 1024}MB")
            
            # --- PHASE 0: FIND ALL SEEDS (SIGNATURE ONLY) ---
            print("\n=== PHASE 0: Finding ALL Seeds (Signature Only) ===")
            
            all_seeds = self.find_all_seeds(SCAN_START, SCAN_END, SIG_TEXT)
            
            if not all_seeds:
                print("No seeds found.")
                return

            print(f"\nTotal seeds found: {len(all_seeds)}")
            
            # --- PHASE 1: COMPREHENSIVE WINDOW SCANS (SIGNATURE ONLY) ---
            print("\n=== PHASE 1: Comprehensive Window Scans (Signature Only) ===")
            
            all_logs = []
            seen_addresses = set()
            
            for i, seed in enumerate(all_seeds, 1):
                print(f"\nProcessing seed {i}/{len(all_seeds)}: 0x{seed:x}")
                window_logs = self.comprehensive_window_scan(seed, WINDOW_SIZE, SIG_TEXT)
                
                for addr, text in window_logs:
                    if addr not in seen_addresses:
                        all_logs.append((addr, text))
                        seen_addresses.add(addr)
                
                print(f"    Found {len(window_logs)} logs (unique so far: {len(all_logs)})")
            
            # Sort logs by address
            all_logs.sort(key=lambda x: x[0])
            
            # --- FINAL RESULTS ---
            print("\n" + "=" * 60)
            print("FINAL RESULTS")
            print("=" * 60)
            print(f"Total unique 'Ship spotted' logs found: {len(all_logs)}")
            print(f"Scanned {len(all_seeds)} regions with {WINDOW_SIZE // 1024 // 1024}MB windows")
            print("-" * 60)
            
            for addr, text in all_logs:
                print(f"0x{addr:x}: {text}")
            
            print("-" * 60)
            print("Scan complete!")
                
        except Exception as e:
            print(f"\nCritical Error: {e}")
            import traceback
            traceback.print_exc()

ScanLogStructs()
