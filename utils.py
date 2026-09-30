#!/usr/bin/env python3
"""
Shared utility functions for SH3 memory monitoring.
Used by all collector scripts.
"""
import os
import struct
import json
import re
from datetime import datetime

PROCESS_NAME = "sh3.exe"

def get_pid_by_name(name=None):
    """
    Finds the Process ID (PID) for a given process name.
    Defaults to PROCESS_NAME if no name provided.
    """
    if name is None:
        name = PROCESS_NAME
    
    try:
        for pid_dir in os.listdir('/proc'):
            if pid_dir.isdigit():
                try:
                    with open(f"/proc/{pid_dir}/cmdline", 'r') as f:
                        cmdline = f.read()
                        if name in cmdline:
                            return int(pid_dir)
                except (IOError, ProcessLookupError):
                    continue
    except FileNotFoundError:
        pass
    return None

def read_memory(pid, address, size=4):
    """
    Reads 'size' bytes from memory at 'address' for process 'pid'.
    Returns empty bytes if read fails.
    """
    mem_path = f"/proc/{pid}/mem"
    try:
        with open(mem_path, 'rb') as mem_file:
            mem_file.seek(address)
            return mem_file.read(size)
    except IOError as e:
        # Occasionally reading /proc/mem fails if the page is swapped or invalid
        return b''

def get_value(ptr_addr, offset, pid=None):
    """
    Generic function to read a float via pointer + offset.
    If pid is not provided, it will be looked up automatically.
    """
    if pid is None:
        pid = get_pid_by_name()
        if not pid:
            raise Exception(f"Could not find process '{PROCESS_NAME}'")

    ptr_bytes = read_memory(pid, ptr_addr, 4)
    if not ptr_bytes: 
        return 0.0
    
    base_address = struct.unpack('<I', ptr_bytes)[0]
    
    target_address = base_address + offset
    val_bytes = read_memory(pid, target_address, 4)
    if not val_bytes: 
        return 0.0
    
    return struct.unpack('<f', val_bytes)[0]

def read_memory_chunk(pid, address, size):
    """
    Reads a larger chunk of memory at once.
    Useful for scanning operations.
    """
    mem_path = f"/proc/{pid}/mem"
    try:
        with open(mem_path, 'rb') as mem_file:
            mem_file.seek(address)
            return mem_file.read(size)
    except IOError:
        return b''


# In-game clock UI string (as shown when hovering the HUD clock):
# "September 5, 1940\nLocal time: 07:23". The date/time are boat-LOCAL;
# a static template "\nLocal time: %02d:%02d" also exists and is rejected
# by requiring actual digits.
UI_CLOCK_RE = re.compile(
    rb'([A-Z][a-z]+) (\d{1,2}), (19[34][0-9])\r?\nLocal time: (\d{1,2}):(\d{2})')
MONTHS = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5,
          'June': 6, 'July': 7, 'August': 8, 'September': 9, 'October': 10,
          'November': 11, 'December': 12}


def iter_rw_regions(pid):
    """Enumerate (start, end) rw memory regions of the process."""
    regions = []
    with open(f'/proc/{pid}/maps') as mf:
        for line in mf:
            parts = line.split()
            if 'rw' not in parts[1]:
                continue
            s, e = parts[0].split('-')
            regions.append((int(s, 16), int(e, 16)))
    return regions


def iter_memory_chunks(pid, chunk_size=1 << 22, overlap=0):
    """
    Yield (chunk_addr, bytes) for every rw region of the process, read in
    large chunks from a single open file handle. `overlap` extra bytes are
    read past each chunk so patterns spanning chunk boundaries are seen.
    """
    with open(f'/proc/{pid}/mem', 'rb', buffering=0) as mem:
        for s, e in iter_rw_regions(pid):
            for off in range(s, e, chunk_size):
                n = min(chunk_size + overlap, e - off)
                try:
                    mem.seek(off)
                    buf = mem.read(n)
                except OSError:
                    continue
                if buf:
                    yield off, buf


def find_ui_clock(pid):
    """
    Find the live in-game clock UI string.

    Returns ((year, month, day), local_minutes_of_day) in boat-local time,
    or None if not found.
    """
    for off, buf in iter_memory_chunks(pid, 1 << 22, 128):
        i = 0
        while True:
            i = buf.find(b'\nLocal time: ', i)
            if i == -1:
                break
            ctx = buf[max(0, i - 64):i + 32]
            m = UI_CLOCK_RE.search(ctx)
            if m:
                month = MONTHS.get(m.group(1).decode('ascii', errors='replace'))
                if month:
                    day, year = int(m.group(2)), int(m.group(3))
                    if 1 <= day <= 31:
                        lh, lm = int(m.group(4)), int(m.group(5))
                        return (year, month, day), lh * 60 + lm
            i += 1
    return None


def read_log_string(pid, address):
    """
    Reads a full null-terminated string starting at address.
    Returns decoded string or empty string on failure.
    """
    try:
        # Read 512 bytes (enough for a log line or a long multi-line radio message)
        data = read_memory_chunk(pid, address, 512)
        if not data:
            return ""
        
        # Find null terminator
        try:
            null_index = data.index(b'\x00')
            raw_string = data[:null_index]
        except ValueError:
            raw_string = data
        
        # Decode
        return raw_string.decode('utf-8', errors='replace')
    except Exception:
        return ""


def validate_log_format(text):
    """
    Validates that the log text matches expected format for ship spotted or aircraft spotted messages.
    
    Args:
        text: Log text to validate
    
    Returns:
        True if text is valid, False otherwise
    """
    # Check length - logs should be reasonable length
    if len(text) > 100 or len(text) < 30:
        return False
    
    # Check that it starts with expected text (either ship or aircraft spotted)
    if not (text.startswith("Ship spotted, sir!") or text.startswith("Aircraft spotted, sir!")):
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


def validate_radio_message_format(text):
    """
    Validates that the text matches the expected format for a radio message body
    as stored in SH3's message history, e.g.:
        From: U-56\nTo: BdU\n27/VIII \nAM 0273 \n Harms
        From: Lehmann Radio News\nTo: on air\n26/VIII \nThe Royal Air Force bombed Berlin yesterday.
        From: BdU\nTo: U-45\nContact Reported. Enemy Task Force , Grid AN 18 , Course SSE, Speed 8 KTS!

    Rejects format-string templates (e.g. 'From: %s') and mission-order strings
    (e.g. 'From: Wilhelmshaven\nMission Orders: ...').

    Args:
        text: Log text to validate

    Returns:
        True if text is a valid radio message body, False otherwise
    """
    if len(text) < 15 or len(text) > 600:
        return False
    if not text.startswith("From:"):
        return False
    if "\nTo:" not in text:
        return False
    if "%s" in text:
        return False
    return True


def fast_seed_scan(pid, scan_start, scan_end, sig_text):
    """
    Fast full-memory scan for one or more signatures using a persistent file
    handle and 1MB chunk reads. Skips unreadable (unmapped) regions.
    ~0.7s for the full 0x08048000-0xC0000000 range vs ~4.3s for find_all_seeds.

    Args:
        pid: Process ID
        scan_start: Start address for scan
        scan_end: End address for scan
        sig_text: Signature text bytes, or a list of signatures (single pass)

    Returns:
        List of raw seed addresses (no deduplication)
    """
    if isinstance(sig_text, (bytes, bytearray)):
        sigs = [bytes(sig_text)]
    else:
        sigs = [bytes(s) for s in sig_text]
    seeds = []
    chunk_size = 1024 * 1024  # 1MB
    try:
        with open(f'/proc/{pid}/mem', 'rb') as mem:
            addr = scan_start
            while addr < scan_end:
                try:
                    mem.seek(addr)
                    data = mem.read(min(chunk_size, scan_end - addr))
                except (IOError, OSError):
                    addr += chunk_size
                    continue
                if not data:
                    addr += chunk_size
                    continue
                for sig in sigs:
                    offset = 0
                    while True:
                        idx = data.find(sig, offset)
                        if idx == -1:
                            break
                        seeds.append(addr + idx)
                        offset = idx + 1
                addr += len(data)
    except (IOError, OSError):
        pass
    return seeds


def find_string_start(pid, seed, max_back=512):
    """
    Given a seed address that points into the middle of a log string, walk
    backwards to find the start of the containing string. Log strings contain
    only printable ASCII and newlines; the byte before the string start is
    part of the record header (non-printable), so the first non-string byte
    going backwards marks the boundary.

    Args:
        pid: Process ID
        seed: Address of a signature match inside a string
        max_back: Maximum bytes to walk back (strings are short)

    Returns:
        Address of the start of the containing string (or the seed itself
        if the boundary cannot be found)
    """
    window_start = max(0, seed - max_back)
    data = read_memory_chunk(pid, window_start, seed - window_start + 1)
    if not data:
        return seed
    # data[-1] is the byte at the seed itself; walk backwards from before it
    for i in range(len(data) - 2, -1, -1):
        b = data[i]
        if not (b == 0x0a or 0x20 <= b < 0x7f):
            return window_start + i + 1
    return window_start


def read_logs_from_seeds(pid, seeds, filter_text=None, validator=None, backtrack=False,
                          reject_regions=None):
    """
    Reads and validates log strings at the given seed addresses.
    Applies content deduplication (same text at multiple addresses counts once).

    Args:
        pid: Process ID
        seeds: List of seed addresses where signature was found
        filter_text: Optional text filter
        validator: Optional validator function (defaults to validate_log_format)
        backtrack: If True, walk backwards from each seed to the start of the
            containing string before reading (for signatures that match
            mid-string, e.g. captain's log entries)
        reject_regions: Optional list of (start, end) address ranges to ignore
            (e.g. static data blocks containing format templates)

    Returns:
        List of (address, text) tuples, deduplicated by content
    """
    logs = []
    seen_content = set()
    validate = validator or validate_log_format
    for addr in seeds:
        try:
            if reject_regions and any(rs <= addr < re_ for rs, re_ in reject_regions):
                continue
            if backtrack:
                addr = find_string_start(pid, addr)
            text = read_log_string(pid, addr)
            if validate(text):
                if filter_text is None or filter_text in text:
                    if text not in seen_content:
                        logs.append((addr, text))
                        seen_content.add(text)
        except Exception:
            pass
    return logs


def find_all_seeds(pid, scan_start, scan_end, sig_text, progress_callback=None):
    """
    Finds all seed addresses in the memory range using signature only.
    Optimized with bytes.find() for 10-100x performance improvement.
    
    Args:
        pid: Process ID
        scan_start: Start address for scan
        scan_end: End address for scan
        sig_text: Signature text bytes
        progress_callback: Optional callback function called with progress info
    
    Returns:
        List of seed addresses
    """
    print(f"[find_all_seeds] Starting memory scan from 0x{scan_start:x} to 0x{scan_end:x}")
    seeds = []
    current_page = scan_start
    total_pages = (scan_end - scan_start) // 4096
    pages_processed = 0
    last_progress = 0
    
    while current_page < scan_end:
        page_data = read_memory_chunk(pid, current_page, 4096)
        
        if page_data and len(page_data) >= len(sig_text):
            # Use optimized bytes.find() instead of naive byte-by-byte scan
            # This is implemented in C and uses efficient algorithms (similar to Boyer-Moore)
            offset = 0
            sig_len = len(sig_text)
            limit = len(page_data) - sig_len
            
            while offset <= limit:
                idx = page_data.find(sig_text, offset)
                if idx == -1:
                    break  # No more matches in this page
                
                candidate_addr = current_page + idx
                
                # Skip if already in our seed list (within reasonable proximity)
                is_duplicate = False
                for seed in seeds:
                    if abs(candidate_addr - seed) < 0x1000:  # Same page
                        is_duplicate = True
                        break
                
                if not is_duplicate:
                    seeds.append(candidate_addr)
                
                offset = idx + 1  # Continue search from next byte
        
        current_page += 4096
        pages_processed += 1
        
        # Report progress every 10% or every 10,000 pages
        if progress_callback:
            percent = (pages_processed / total_pages) * 100
            if percent - last_progress >= 10 or pages_processed % 10000 == 0:
                progress_callback({
                    'phase': 1,
                    'phase_name': 'Finding seeds',
                    'progress': percent,
                    'seeds_found': len(seeds),
                    'pages_processed': pages_processed,
                    'total_pages': total_pages
                })
                last_progress = percent
    
    print(f"[find_all_seeds] Completed scan. Found {len(seeds)} seed addresses")
    return sorted(seeds)


def comprehensive_window_scan(pid, seed, window_size, sig_text, progress_callback=None, seed_index=None, total_seeds=None, validator=None):
    """
    Performs a comprehensive scan of a window around a seed address.
    Optimized with chunked reads and bytes.find() for 10-100x performance improvement.
    
    Args:
        pid: Process ID
        seed: Seed address (center of window)
        window_size: Total window size to scan (half on each side)
        sig_text: Signature text bytes
        progress_callback: Optional callback function called with progress info
        seed_index: Current seed index (for progress reporting)
        total_seeds: Total number of seeds (for progress reporting)
    
    Returns:
        List of (address, text) tuples for matching logs
    """
    logs = []
    print(f"[comprehensive_window_scan] Scanning seed {seed_index+1}/{total_seeds} at 0x{seed:x}")
    scan_start = seed - window_size
    scan_end = seed + window_size
    
    # Report progress for this seed
    if progress_callback and seed_index is not None and total_seeds is not None:
        progress_callback({
            'phase': 2,
            'phase_name': 'Scanning windows',
            'progress': ((seed_index + 1) / total_seeds) * 100,
            'current_seed': seed_index + 1,
            'total_seeds': total_seeds,
            'seed_address': f"0x{seed:x}"
        })
    
    # Use optimized chunked reads instead of byte-by-byte
    CHUNK_SIZE = 4096  # Read 4KB chunks
    sig_len = len(sig_text)
    
    current = scan_start
    
    while current < scan_end:
        # Read a chunk of memory
        chunk_data = read_memory_chunk(pid, current, min(CHUNK_SIZE, scan_end - current))
        
        if chunk_data and len(chunk_data) >= sig_len:
            # Use bytes.find() to find all matches in this chunk
            offset = 0
            limit = len(chunk_data) - sig_len
            
            while offset <= limit:
                idx = chunk_data.find(sig_text, offset)
                if idx == -1:
                    break  # No more matches in this chunk
                
                # Calculate absolute address
                absolute_addr = current + idx
                
                try:
                    text = read_log_string(pid, absolute_addr)
                    # Validate format to filter out garbage
                    validate = validator or validate_log_format
                    if validate(text):
                        logs.append((absolute_addr, text))
                except Exception:
                    pass
                
                offset = idx + 1  # Continue search from next byte
        
        current += CHUNK_SIZE
    
    print(f"[comprehensive_window_scan] Completed scan of seed 0x{seed:x}. Found {len(logs)} logs")
    return logs


def discover_log_array(pid, sig_text, scan_start=0x08048000, scan_end=0xC0000000, 
                      search_window=0x200000, progress_callback=None, validator=None):
    """
    Performs comprehensive memory scan to find all log arrays.
    Uses signature-only validation with format checking.
    
    Args:
        pid: Process ID
        sig_text: Signature text to search for (bytes)
        scan_start: Start address for scan
        scan_end: End address for scan
        search_window: Window size around seed for detailed search (half on each side)
        progress_callback: Optional callback function called with progress info
    
    Returns:
        Tuple of (list_of_addresses, list_of_logs) where:
        - list_of_addresses: List of all unique log addresses found
        - list_of_logs: List of (address, text) tuples
    """
    mem_path = f"/proc/{pid}/mem"
    print(f"[discover_log_array] Starting discovery for signature: {sig_text}")
    
    if not os.path.exists(mem_path):
        raise Exception(f"Memory path {mem_path} does not exist. Run with sudo.")
    
    # --- PHASE 0: FIND ALL SEEDS (SIGNATURE ONLY) ---
    print(f"[discover_log_array] Phase 0: Finding seeds...")
    all_seeds = find_all_seeds(pid, scan_start, scan_end, sig_text, progress_callback)
    
    if not all_seeds:
        print(f"[discover_log_array] ERROR: No seeds found")
        raise Exception("Failed to find any log seeds in memory.")
    
    # --- PHASE 1: COMPREHENSIVE WINDOW SCANS (SIGNATURE ONLY) ---
    print(f"[discover_log_array] Phase 1: Scanning windows around {len(all_seeds)} seeds...")
    all_logs = []
    seen_addresses = set()
    
    for i, seed in enumerate(all_seeds):
        window_logs = comprehensive_window_scan(pid, seed, search_window, sig_text, 
                                               progress_callback, i, len(all_seeds),
                                               validator=validator)
        
        for addr, text in window_logs:
            if addr not in seen_addresses:
                all_logs.append((addr, text))
                seen_addresses.add(addr)
    
    # Sort logs by address
    all_logs.sort(key=lambda x: x[0])
    
    print(f"[discover_log_array] Discovery complete. Found {len(all_logs)} valid logs at {len(seen_addresses)} unique addresses")
    
    # Return list of all unique addresses and the logs
    if all_logs:
        unique_addresses = sorted(seen_addresses)
        return unique_addresses, all_logs
    else:
        print(f"[discover_log_array] ERROR: Found seeds but no valid logs in any window")
        raise Exception("Found seeds but no valid logs in any window.")


def read_logs_from_address(pid, monitor_addresses, sig_text, search_window=0x200000, filter_text=None, validator=None):
    """
    Reads all logs from the cached address locations.
    Uses signature-only validation with format checking.
    Optimized with chunked reads and bytes.find() for 10-100x performance improvement.
    
    Args:
        pid: Process ID
        monitor_addresses: List of base addresses to search around
        sig_text: Signature text to search for (bytes)
        search_window: Window size around each address (half on each side)
        filter_text: Optional text to filter logs (only include logs containing this text)
    
    Returns:
        List of (address, text) tuples
    """
    all_logs = []
    seen_addresses = set()
    
    # Use optimized chunked reads
    CHUNK_SIZE = 4096
    sig_len = len(sig_text)
    
    # Scan windows around each cached address
    for base_address in monitor_addresses:
        scan_start = base_address - search_window
        scan_end = base_address + search_window
        
        current = scan_start
        
        while current < scan_end:
            # Read a chunk of memory
            chunk_data = read_memory_chunk(pid, current, min(CHUNK_SIZE, scan_end - current))
            
            if chunk_data and len(chunk_data) >= sig_len:
                # Use bytes.find() to find all matches in this chunk
                offset = 0
                limit = len(chunk_data) - sig_len
                
                while offset <= limit:
                    idx = chunk_data.find(sig_text, offset)
                    if idx == -1:
                        break  # No more matches in this chunk
                    
                    # Calculate absolute address
                    absolute_addr = current + idx
                    
                    try:
                        text = read_log_string(pid, absolute_addr)
                        # Validate format
                        validate = validator or validate_log_format
                        if validate(text):
                            if filter_text is None or filter_text in text:
                                if absolute_addr not in seen_addresses:
                                    all_logs.append((absolute_addr, text))
                                    seen_addresses.add(absolute_addr)
                    except Exception:
                        pass
                    
                    offset = idx + 1  # Continue search from next byte
            
            current += CHUNK_SIZE
    
    all_logs.sort(key=lambda x: x[0])
    return all_logs


def clean_log_message(text, target_text):
    """
    Cleans and formats log message.
    
    Args:
        text: Raw log message
        target_text: Target text to extract from the log
    
    Returns:
        Cleaned and formatted log message
    """
    text = text.replace('', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    
    parts = text.split(target_text)
    
    if len(parts) > 1:
        prefix = parts[0].strip()
        suffix = parts[1].strip()
        
        time_match = re.search(r'(\d{1,2}:\d{2})', prefix)
        timestamp = time_match.group(1) if time_match else ""
        
        message = target_text + " " + suffix[:50]
        message = re.sub(r'[^a-zA-Z0-9\s\.,!\?]', '', message).strip()
        
        if timestamp:
            return f"[{timestamp}] {message}"
        else:
            return message
            
    return text


def save_cache(cache_file, log_type, pid, addresses):
    """
    Saves PID and list of addresses to cache file for a specific log type.
    
    Args:
        cache_file: Path to cache file
        log_type: Type of log (e.g., 'ship_spotted', 'message_received')
        pid: Process ID
        addresses: List of discovered addresses
    """
    # Load existing cache if it exists
    cache_data = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
        except Exception:
            cache_data = {}
    
    # Update cache for this log type
    cache_data[log_type] = {
        'pid': pid,
        'addresses': addresses,
        'timestamp': datetime.now().isoformat()
    }
    
    with open(cache_file, 'w') as f:
        json.dump(cache_data, f, indent=2)


def load_cache(cache_file, log_type):
    """
    Loads cache file for a specific log type if it exists.
    
    Args:
        cache_file: Path to cache file
        log_type: Type of log to load
    
    Returns:
        Dictionary with 'pid', 'addresses', 'timestamp' or None if not found
    """
    if not os.path.exists(cache_file):
        return None
    
    try:
        with open(cache_file, 'r') as f:
            cache_data = json.load(f)
            return cache_data.get(log_type)
    except Exception:
        return None
