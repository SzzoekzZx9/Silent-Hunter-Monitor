#!/usr/bin/env python3
"""
Unified Log Collector
Generic collector for monitoring SH3 game memory for various log messages.
Can be configured to monitor different log types (ship spotted, message received, etc.)
"""
import sys
import os
import time
import subprocess
import threading

# Import shared utilities and event bus
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus

# Global lock for PID lookup to prevent race conditions
_pid_lookup_lock = threading.Lock()


class LogCollectorConfig:
    """Configuration for a log collector."""
    def __init__(self, log_type, sig_text, sig_header=None, stride=None, filter_text=None, 
                 event_type=None, cache_file='.sh3_log_cache.json', monitor_interval=5,
                 scan_start=0x08048000, scan_end=0xC0000000, search_window=0x200000,
                 rediscovery_interval=300, validator=None, publish_initial_logs=False,
                 fast_scan=True, sig_at_start=True, reject_regions=None):
        self.log_type = log_type
        self.sig_text = sig_text
        # sig_header and stride are kept for backward compatibility but not used
        self.sig_header = sig_header
        self.stride = stride
        self.filter_text = filter_text
        self.event_type = event_type
        self.cache_file = cache_file
        self.monitor_interval = monitor_interval
        self.scan_start = scan_start
        self.scan_end = scan_end
        self.search_window = search_window
        self.rediscovery_interval = rediscovery_interval  # Seconds between full rediscoveries (default: 5 minutes)
        self.validator = validator  # Optional custom validator function; defaults to utils.validate_log_format
        self.publish_initial_logs = publish_initial_logs  # If True, publish logs found on initial discovery/cache load
        self.fast_scan = fast_scan  # If True, monitor via full-memory fast seed scans (~0.7s) each poll
                                     # instead of window scans around cached addresses
        self.sig_at_start = sig_at_start  # If True (default), signatures match at the start of the
                                          # log string. If False, seeds may point mid-string and the
                                          # reader backtracks to the containing string's start.
        self.reject_regions = reject_regions or []  # Address ranges to ignore (e.g. static
                                                     # data blocks with format templates)


class LogCollector:
    """Generic log collector that monitors memory for specific log messages."""
    
    def __init__(self, config):
        """
        Initialize the log collector.
        
        Args:
            config: LogCollectorConfig instance with collector settings
        """
        self.config = config
        self.validator = config.validator  # custom validator or None (defaults to validate_log_format)
        self.publish_initial_logs = config.publish_initial_logs
        self.fast_scan = config.fast_scan  # If True, monitor via full-memory fast seed scans each poll
        self.sig_at_start = config.sig_at_start  # If False, backtrack from seeds to string start when reading
        self.reject_regions = config.reject_regions  # Address ranges to ignore when reading seeds
        self.pid = None
        self.monitor_addresses = None  # List of addresses to monitor
        self.seen_logs = {}  # Dictionary: address -> text (to track changes at each address)
        self.seen_log_contents = set()  # Set of all log content we've published (for deduplication)
        self.last_rediscovery_time = 0  # Timestamp of last full rediscovery
        self._last_progress_time = 0  # Throttle progress events
        self.first_discovery_complete = False  # Flag to skip notifications on initial run
        # Error state tracking for notification consolidation
        self._last_error_message = None  # Last error message sent as notification
        self._consecutive_failures = 0  # Count of consecutive failures for exponential backoff
        self._awaiting_logs = False  # Flag indicating we're waiting for logs (no error state)
    
    def get_pid(self):
        """Get the PID of the SH3 process."""
        # Use lock to prevent race conditions when multiple threads call this simultaneously
        with _pid_lookup_lock:
            try:
                return int(subprocess.check_output(["pgrep", "-f", "sh3.exe"]).strip().decode())
            except:
                try:
                    return int(input("Enter PID: "))
                except:
                    return None
    
    def _progress_callback(self, progress_info):
        """
        Callback for progress updates during discovery.
        Publishes STATUS events with progress information.
        
        Args:
            progress_info: Dictionary with progress information
        """
        import time
        current_time = time.time()
        
        # Throttle progress events to avoid flooding (max 1 per second)
        if current_time - self._last_progress_time < 1.0:
            return
        
        self._last_progress_time = current_time
        
        phase = progress_info.get('phase')
        phase_name = progress_info.get('phase_name', '')
        progress = progress_info.get('progress', 0)
        
        if phase == 1:
            # Phase 1: Finding seeds
            seeds_found = progress_info.get('seeds_found', 0)
            pages_processed = progress_info.get('pages_processed', 0)
            total_pages = progress_info.get('total_pages', 0)
            msg = (f"Searching {self.config.log_type}: {phase_name} "
                   f"{progress:.0f}% - Found {seeds_found} seeds "
                   f"({pages_processed}/{total_pages} pages)")
            event_bus.publish_event(event_bus.EventTypes.STATUS, msg)
            
        elif phase == 2:
            # Phase 2: Scanning windows
            current_seed = progress_info.get('current_seed', 0)
            total_seeds = progress_info.get('total_seeds', 0)
            seed_address = progress_info.get('seed_address', '')
            msg = (f"Searching {self.config.log_type}: {phase_name} "
                   f"{progress:.0f}% - Scanning seed {current_seed}/{total_seeds} "
                   f"at {seed_address}")
            event_bus.publish_event(event_bus.EventTypes.STATUS, msg)
    
    def _publish_error(self, error_message):
        """
        Publish error notification with deduplication.
        Only publishes if the error message has changed from the last one.
        
        Args:
            error_message: The error message to publish
            
        Returns:
            True if the error was published, False if it was a duplicate
        """
        # Check if this is a new error message
        if self._last_error_message != error_message:
            self._last_error_message = error_message
            event_bus.publish_event(event_bus.EventTypes.ERROR, error_message)
            return True
        return False
    
    def _get_backoff_delay(self):
        """
        Calculate exponential backoff delay based on consecutive failures.
        
        Returns:
            Delay in seconds (30s, 60s, 120s, 300s max)
        """
        # Exponential backoff: 30s, 60s, 120s, 300s (capped at 5 minutes)
        base_delay = 30
        max_delay = 300
        delay = min(base_delay * (2 ** min(self._consecutive_failures, 3)), max_delay)
        return delay
    
    def _reset_error_state(self):
        """Reset error state tracking after a successful operation."""
        self._last_error_message = None
        self._consecutive_failures = 0
    
    def initialize(self):
        """
        Initialize the collector by loading cache or performing discovery.
        
        Returns:
            True if successful, False otherwise
        """
        self.pid = self.get_pid()
        if not self.pid:
            return False
        
        # Fast scan mode: the first fast scan handles initialization
        # (populates seen logs, saves cache, publishes SEARCH_STATUS).
        # No cache loading or slow discovery needed - the monitoring loop
        # calls check_for_new_logs_fast() every interval.
        if self.fast_scan:
            self.check_for_new_logs_fast()
            return self.first_discovery_complete
        
        # Check cache
        cache = utils.load_cache(self.config.cache_file, self.config.log_type)
        
        if cache and cache['pid'] == self.pid:
            # Check cache age - if too old, force rediscovery
            cache_age = 0
            try:
                from datetime import datetime
                cache_time = datetime.fromisoformat(cache['timestamp'])
                cache_age = (datetime.now() - cache_time).total_seconds()
            except:
                pass
            
            # Force rediscovery if cache is older than 1 hour
            if cache_age > 3600:
                cache = None
            else:
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"Using cached addresses for {self.config.log_type}")
        
        if cache and cache['pid'] == self.pid:
            # Use cached addresses
            self.monitor_addresses = cache.get('addresses', [])
            
            if not self.monitor_addresses:
                # Fall back to discovery if no addresses in cache
                self.monitor_addresses = None
                return self._perform_discovery()
            
            try:
                # Read initial logs
                initial_logs = utils.read_logs_from_address(
                    self.pid, 
                    self.monitor_addresses,
                    self.config.sig_text,
                    self.config.search_window,
                    self.config.filter_text,
                    validator=self.validator
                )
                
                for addr, text in initial_logs:
                    self.seen_logs[addr] = text
                    if text not in self.seen_log_contents:
                        self.seen_log_contents.add(text)
                        if self.publish_initial_logs:
                            event_bus.publish_event(self.config.event_type, text)
                
                return True
                
            except Exception as e:
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                                       f"Failed to load cache for {self.config.log_type}: {e}, will retry discovery")
                self.monitor_addresses = None
        
        if self.monitor_addresses is None:
            # Discovery mode - publish SEARCH_STATUS event
            event_bus.publish_event(event_bus.EventTypes.SEARCH_STATUS, {
                'log_type': self.config.log_type,
                'status': 'searching',
                'count': 0
            })
            return self._perform_discovery()
        
        return False
    
    def _perform_discovery(self):
        """
        Perform memory discovery to find all log arrays.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Publish start event
            event_bus.publish_event(event_bus.EventTypes.STATUS,
                                   f"Starting discovery for {self.config.log_type} logs...")
            
            # Publish SEARCH_STATUS event for UI
            event_bus.publish_event(event_bus.EventTypes.SEARCH_STATUS, {
                'log_type': self.config.log_type,
                'status': 'searching',
                'count': 0
            })
            
            self.monitor_addresses, initial_logs = utils.discover_log_array(
                self.pid,
                self.config.sig_text,
                self.config.scan_start,
                self.config.scan_end,
                self.config.search_window,
                progress_callback=self._progress_callback,
                validator=self.validator
            )
            
            # Publish completion event
            event_bus.publish_event(event_bus.EventTypes.STATUS,
                                   f"Discovery complete! Found {len(self.monitor_addresses)} log arrays for {self.config.log_type}")
            
            # Publish SEARCH_STATUS event for UI
            event_bus.publish_event(event_bus.EventTypes.SEARCH_STATUS, {
                'log_type': self.config.log_type,
                'status': 'complete',
                'count': len(self.monitor_addresses)
            })
            
            # Save to cache
            utils.save_cache(self.config.cache_file, self.config.log_type, 
                            self.pid, self.monitor_addresses)
            
            # Add initial logs to seen dict - MERGE instead of overwrite to preserve deduplication
            # Also check for new logs and publish notifications (skip notifications on first discovery)
            new_logs_found = []
            for addr, text in initial_logs:
                # Only update if this is a new address or text has changed
                if addr not in self.seen_logs or self.seen_logs[addr] != text:
                    # Check if this log content is new (not in seen_log_contents)
                    if text not in self.seen_log_contents:
                        new_logs_found.append(text)
                        self.seen_log_contents.add(text)
                    
                    self.seen_logs[addr] = text
            
            # Only publish notifications for new logs if this is NOT the first discovery
            # (skip notifying about all existing logs on initial run)
            if new_logs_found and (self.first_discovery_complete or self.publish_initial_logs):
                for log in new_logs_found:
                    event_bus.publish_event(self.config.event_type, log)
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: Found {len(new_logs_found)} new logs during discovery!")
            
            # Mark first discovery as complete after processing logs
            self.first_discovery_complete = True
            
            # Reset error state and awaiting logs flag on successful discovery
            self._reset_error_state()
            self._awaiting_logs = False
            
            return True
            
        except Exception as e:
            error_msg = str(e)
            
            # Check if this is a "no logs found" scenario (normal for some collectors)
            is_no_logs_scenario = (
                "Failed to find any log seeds in memory" in error_msg or
                "Found seeds but no valid logs in any window" in error_msg
            )
            
            if is_no_logs_scenario:
                # This is expected behavior for collectors that haven't spotted anything yet
                # Treat as INFO/STATUS, not ERROR
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: No logs found yet (expected if no {self.config.log_type.replace('_', ' ')} events have occurred)")
                
                # Set empty address list to indicate we're waiting for logs
                self.monitor_addresses = []
                
                # Set awaiting logs flag to suppress periodic error notifications
                self._awaiting_logs = True
                
                # Publish SEARCH_STATUS event for UI
                event_bus.publish_event(event_bus.EventTypes.SEARCH_STATUS, {
                    'log_type': self.config.log_type,
                    'status': 'no_logs',
                    'count': 0
                })
                return False
            else:
                # This is an actual error (game not running, memory access issue, etc.)
                # Use consolidated error notification
                self._publish_error(f"{self.config.log_type} collector failed: {e}")
                self._consecutive_failures += 1
                
                # Publish SEARCH_STATUS event for UI
                event_bus.publish_event(event_bus.EventTypes.SEARCH_STATUS, {
                    'log_type': self.config.log_type,
                    'status': 'failed',
                    'count': 0
                })
                return False
    
    def _ensure_pid(self):
        """
        Verify the game process is still alive; re-acquire the PID if it died.
        Returns True if self.pid refers to a live process.
        """
        if self.pid and os.path.exists(f'/proc/{self.pid}'):
            return True
        # Re-acquire via pgrep only - never block on input() from a monitor thread
        try:
            new_pid = int(subprocess.check_output(["pgrep", "-f", "sh3.exe"]).strip().splitlines()[0])
        except Exception:
            new_pid = None
        if new_pid:
            if new_pid != self.pid:
                # New game session: reset seen state so logs from the new
                # session are treated as fresh content
                self.pid = new_pid
                self.seen_logs.clear()
                self.seen_log_contents.clear()
                self.first_discovery_complete = False
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: game process restarted (new PID {new_pid}), resetting state")
            return True
        return False

    def check_for_new_logs_fast(self):
        """
        Check for new log entries using a full-memory fast seed scan.

        Instead of scanning windows around cached addresses (which misses
        entries allocated in new heap regions), this scans the entire game
        memory range for the signature (~0.7s with a persistent /proc/PID/mem
        handle and 1MB chunks), then reads and validates the string at each
        seed. New entries are detected within one poll interval regardless
        of where the game allocated them.

        Returns:
            List of new log messages found
        """
        try:
            if not self._ensure_pid():
                self._publish_error(f"{self.config.log_type}: game process not found")
                self._consecutive_failures += 1
                return []

            seeds = utils.fast_seed_scan(
                self.pid,
                self.config.scan_start,
                self.config.scan_end,
                self.config.sig_text
            )
            current_logs = utils.read_logs_from_seeds(
                self.pid,
                seeds,
                self.config.filter_text,
                validator=self.validator,
                backtrack=not self.config.sig_at_start,
                reject_regions=self.config.reject_regions
            )

            # Refresh monitored addresses (kept for cache/UI compatibility)
            new_addresses = [addr for addr, _ in current_logs]
            if new_addresses != self.monitor_addresses:
                self.monitor_addresses = new_addresses
                utils.save_cache(self.config.cache_file, self.config.log_type,
                                self.pid, self.monitor_addresses)

            # Detect new content: new address, changed text at a known
            # address, or content never seen before
            candidate_logs = []
            for addr, text in current_logs:
                if addr not in self.seen_logs or self.seen_logs[addr] != text:
                    candidate_logs.append(text)
                self.seen_logs[addr] = text

            # Content deduplication - only publish truly new log content.
            # On the very first scan, existing logs are marked seen silently
            # (unless publish_initial_logs is set).
            new_logs = []
            initial_run = not self.first_discovery_complete
            for log in candidate_logs:
                if log not in self.seen_log_contents:
                    self.seen_log_contents.add(log)
                    if not initial_run or self.publish_initial_logs:
                        new_logs.append(log)

            if initial_run:
                self.first_discovery_complete = True
                event_bus.publish_event(event_bus.EventTypes.SEARCH_STATUS, {
                    'log_type': self.config.log_type,
                    'status': 'complete' if current_logs else 'no_logs',
                    'count': len(current_logs)
                })
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: fast scan initialized, "
                                       f"tracking {len(current_logs)} existing logs")

            if new_logs:
                for log in new_logs:
                    event_bus.publish_event(self.config.event_type, log)
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: Found {len(new_logs)} new logs!")

            self._reset_error_state()
            return new_logs

        except Exception as e:
            self._publish_error(f"{self.config.log_type} fast scan error: {e}")
            self._consecutive_failures += 1
            return []

    def check_for_new_logs(self):
        """
        Check for new log entries and publish events.
        Implements content deduplication to prevent duplicate notifications.
        
        Returns:
            List of new log messages found
        """
        try:
            # Read current logs
            current_logs = utils.read_logs_from_address(
                self.pid,
                self.monitor_addresses,
                self.config.sig_text,
                self.config.search_window,
                self.config.filter_text,
                validator=self.validator
            )
            
            # Log monitoring activity
            event_bus.publish_event(event_bus.EventTypes.STATUS,
                                   f"Monitoring {self.config.log_type}: Found {len(current_logs)} logs "
                                   f"across {len(self.monitor_addresses)} addresses")
            
            # Check for new logs by comparing text at each address
            candidate_logs = []
            for addr, text in current_logs:
                if addr not in self.seen_logs:
                    # New address we haven't seen before
                    candidate_logs.append(text)
                    self.seen_logs[addr] = text
                elif self.seen_logs[addr] != text:
                    # Text at this address has changed - new log!
                    candidate_logs.append(text)
                    self.seen_logs[addr] = text
            
            # Apply content deduplication - only publish truly new log content
            new_logs = []
            for log in candidate_logs:
                if log not in self.seen_log_contents:
                    new_logs.append(log)
                    self.seen_log_contents.add(log)
            
            # Publish events for new logs
            if new_logs:
                for log in new_logs:
                    event_bus.publish_event(self.config.event_type, log)
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: Found {len(new_logs)} new logs!")
            
            return new_logs
            
        except Exception as e:
            # Use consolidated error notification for monitoring error
            self._publish_error(f"{self.config.log_type} monitoring error: {e}")
            
            try:
                # Try to rediscover
                self.monitor_addresses, initial_logs = utils.discover_log_array(
                    self.pid,
                    self.config.sig_text,
                    self.config.scan_start,
                    self.config.scan_end,
                    self.config.search_window,
                    validator=self.validator
                )
                utils.save_cache(self.config.cache_file, self.config.log_type,
                                self.pid, self.monitor_addresses)
                self.seen_logs.clear()
                for addr, text in initial_logs:
                    self.seen_logs[addr] = text
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"{self.config.log_type}: Rediscovery successful")
                # Reset error state on successful rediscovery
                self._reset_error_state()
            except Exception as e2:
                # Use consolidated error notification for rediscovery failure
                self._publish_error(f"{self.config.log_type} rediscovery failed: {e2}")
                self._consecutive_failures += 1
                # Use exponential backoff delay instead of fixed 30 seconds
                delay = self._get_backoff_delay()
                time.sleep(delay)
            
            return []
    
    def run(self):
        """Main collector loop with periodic full rediscovery."""
        # Attempt initialization but continue even if it fails
        # Periodic rediscovery will retry
        initialize_success = self.initialize()
        
        # Initialize last_rediscovery_time to current time
        self.last_rediscovery_time = time.time()
        
        # Track if we've had a successful discovery to avoid error spam
        has_successful_discovery = initialize_success
        
        # Publish start of monitoring
        event_bus.publish_event(event_bus.EventTypes.STATUS,
                               f"Starting monitoring for {self.config.log_type} logs...")
        
        try:
            check_count = 0
            while True:
                try:
                    check_count += 1
                    
                    # Fast scan mode: full-memory scan every poll interval.
                    # This supersedes window monitoring and periodic rediscovery -
                    # a ~0.7s scan finds new entries wherever they were allocated.
                    if self.config.fast_scan:
                        event_bus.publish_event(event_bus.EventTypes.HEARTBEAT, {
                            'log_type': self.config.log_type,
                            'check_count': check_count
                        })
                        self.check_for_new_logs_fast()
                        time.sleep(self.config.monitor_interval)
                        continue
                    
                    # Check if it's time for periodic full rediscovery
                    current_time = time.time()
                    time_since_rediscovery = current_time - self.last_rediscovery_time
                    
                    if time_since_rediscovery >= self.config.rediscovery_interval:
                        event_bus.publish_event(event_bus.EventTypes.STATUS,
                                               f"Performing periodic full rediscovery for {self.config.log_type} "
                                               f"(every {self.config.rediscovery_interval // 60} minutes)...")
                        
                        # Perform full rediscovery to catch any missed logs and discover new addresses
                        if self._perform_discovery():
                            self.last_rediscovery_time = current_time
                            has_successful_discovery = True
                            event_bus.publish_event(event_bus.EventTypes.STATUS,
                                                   f"Periodic rediscovery complete for {self.config.log_type}")
                        else:
                            # Only publish error if we haven't succeeded yet AND we're not just awaiting logs
                            # (avoid error spam for normal "no logs found" scenarios)
                            if not has_successful_discovery and not self._awaiting_logs:
                                # Use consolidated error notification
                                self._publish_error(f"Periodic rediscovery failed for {self.config.log_type}, will retry later")
                                # Increment failure counter and apply exponential backoff
                                self._consecutive_failures += 1
                                delay = self._get_backoff_delay()
                                time.sleep(delay)
                                # Skip the normal monitor_interval sleep since we just waited with backoff
                                continue
                    
                    # Only check for logs if we have successful discovery and addresses to monitor
                    if self.monitor_addresses and has_successful_discovery:
                        # Publish heartbeat
                        event_bus.publish_event(event_bus.EventTypes.HEARTBEAT, {
                            'log_type': self.config.log_type,
                            'check_count': check_count
                        })
                        
                        # Check for new logs
                        self.check_for_new_logs()
                    else:
                        # Publish heartbeat even without successful discovery
                        event_bus.publish_event(event_bus.EventTypes.HEARTBEAT, {
                            'log_type': self.config.log_type,
                            'check_count': check_count,
                            'discovery_status': 'pending'
                        })
                    
                    time.sleep(self.config.monitor_interval)
                    
                except Exception as e:
                    # Log the error but continue monitoring
                    import traceback
                    error_details = f"{self.config.log_type} monitoring loop error: {e}"
                    # Use consolidated error notification
                    self._publish_error(error_details)
                    event_bus.publish_event(event_bus.EventTypes.STATUS,
                                           f"Traceback: {traceback.format_exc()}")
                    # Use exponential backoff delay instead of fixed 30 seconds
                    self._consecutive_failures += 1
                    delay = self._get_backoff_delay()
                    time.sleep(delay)
                
        except KeyboardInterrupt:
            event_bus.publish_event(event_bus.EventTypes.STATUS,
                                   f"{self.config.log_type} collector stopped")


def run_collector(config):
    """
    Convenience function to run a collector with the given configuration.
    
    Args:
        config: LogCollectorConfig instance
    """
    collector = LogCollector(config)
    collector.run()


if __name__ == "__main__":
    # Example usage (this would be configured by the specific collector scripts)
    print("This is a generic log collector. Use it through specific collector scripts.")
