#!/usr/bin/env python3
"""
SH3 Master Coordinator - NCurses Version
Experimental ncurses-based interface for monitoring SH3 game state.
Features split-screen layout with uboat status and event log.
"""
import sys
import os
import time
import threading
import signal
import curses
from datetime import datetime
from collections import deque

# Load .env (if present) before anything reads configuration from the
# environment, then import event bus and notify
import env_config

env_config.load_env()

import event_bus
import notify

# --- CONFIGURATION ---
# Thresholds for alerts
SHALLOW_DEPTH_THRESHOLD = 100.0  # meters (0-100m zone)
MEDIUM_DEPTH_THRESHOLD = 170.0  # meters (100-170m zone)
HIGH_SPEED_THRESHOLD = 20.0  # knots

# Notification settings
NOTIFY_SHIP_SPOTTED = True
NOTIFY_SHALLOW_DEPTH = False
NOTIFY_HIGH_SPEED = False

# UI Configuration
MAX_LOG_LINES = 100  # Maximum number of log lines to keep
REFRESH_RATE = 0.1  # UI refresh rate in seconds

# Crew screen configuration (story #13)
# Fatigue is 0.0 (fully rested) .. 1.0 (exhausted)
CREW_FATIGUE_MEDIUM = 0.5   # yellow at/above this level
CREW_FATIGUE_HIGH = 0.75    # red at/above this level

# Panel geometry (rows reserved for the status panel and the footer)
STATUS_HEIGHT = 15
FOOTER_HEIGHT = 1
MIN_WIDTH = 100
MIN_HEIGHT = STATUS_HEIGHT + FOOTER_HEIGHT + 5

# While the curses UI is active, stdout/stderr are redirected here so that
# prints from notify() or collector threads cannot corrupt the screen.
UI_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'sh3_master_ui.log')

# State tracking
current_speed = 0.0
current_heading = 0.0
current_depth = 0.0
current_depth_under_keel = 0.0
current_location = None  # dict with 'grid', 'available', 'lat', 'lon_w' or None
last_grid = None          # last grid seen (for crossing events)
current_weather = None   # dict with 'date', 'time', 'text' or None
current_clock = None     # dict with 'date', 'time' (in-game) or None
last_ship_spotted_time = None
last_aircraft_spotted_time = None
radio_messages = deque(maxlen=MAX_LOG_LINES)  # list of received radio messages
captains_log_entries = deque(maxlen=MAX_LOG_LINES)  # captain's log entries
# Panel focus state (Tab cycles: none -> radio -> captain's log)
FOCUS_NONE = 0
FOCUS_RADIO = 1
FOCUS_CAPTAINS = 2
panel_focus = FOCUS_NONE
# Radio panel UI state
radio_selected = 0         # selected index in radio_messages list
radio_scroll = 0           # scroll offset for the radio list
radio_detail_open = False  # True when the message detail overlay is shown
# Captain's log panel UI state
captains_selected = 0      # selected index in captains_log_entries list
captains_scroll = 0        # scroll offset for the captain's log list
captains_detail_open = False  # True when the entry detail overlay is shown
# Crew screen UI state (story #13)
crew_state = None          # latest crew list from CREW_UPDATE events (or demo)
crew_demo = False          # True while crew_state holds demo data
crew_screen_open = False   # True while the crew screen overlay is shown
crew_detail_open = False   # True when a member detail popup is shown on it
crew_selected = 0          # selected index in the crew roster
crew_scroll = 0            # scroll offset for the crew roster
search_status = {
    'ship_spotted': None,  # None, 'searching', 'complete', 'failed'
    'aircraft_spotted': None,
    'message_received': None,
    'captains_log': None
}
search_log_count = {
    'ship_spotted': 0,
    'aircraft_spotted': 0,
    'message_received': 0,
    'captains_log': 0
}
heartbeat_time = {
    'ship_spotted': None,
    'aircraft_spotted': None,
    'message_received': None,
    'captains_log': None
}
last_check_time = {
    'ship_spotted': None,
    'aircraft_spotted': None,
    'message_received': None,
    'captains_log': None
}
event_log = deque(maxlen=MAX_LOG_LINES)
collectors = []  # populated by main(); kept as a module global for main_loop
running = True
# Track previous values for partial refresh optimization
previous_values = {}

class CollectorThread(threading.Thread):
    """Thread wrapper for running collector scripts."""
    
    def __init__(self, name, collector_module):
        super().__init__(name=name, daemon=True)
        self.collector_module = collector_module
        self.running = False
        self.start_time = None
        self.end_time = None
        self.exception = None
        
    def run(self):
        self.start_time = datetime.now()
        self.running = True
        print(f"[{self.start_time.strftime('%H:%M:%S')}] [{self.name}] Thread starting...")
        
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.name}] Calling collector.run()...")
            self.collector_module.run()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.name}] collector.run() completed normally")
            
        except Exception as e:
            self.exception = e
            error_msg = f"ERROR: {self.name} crashed: {e}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.name}] {error_msg}")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.name}] Exception type: {type(e).__name__}")
            add_log_event(error_msg, "error")
            event_bus.publish_event(event_bus.EventTypes.ERROR,
                                   f"{self.name} crashed: {e}")
        finally:
            self.running = False
            self.end_time = datetime.now()
            duration = (self.end_time - self.start_time).total_seconds()
            print(f"[{self.end_time.strftime('%H:%M:%S')}] [{self.name}] Thread stopped (duration: {duration:.2f}s)")
            if self.exception:
                print(f"[{self.end_time.strftime('%H:%M:%S')}] [{self.name}] Exited with exception: {self.exception}")
            else:
                print(f"[{self.end_time.strftime('%H:%M:%S')}] [{self.name}] Exited cleanly")

def add_log_event(message, event_type="info"):
    """Add an event to the log with timestamp and type."""
    timestamp = datetime.now().strftime('%H:%M:%S')
    event_log.append({
        'timestamp': timestamp,
        'message': message,
        'type': event_type
    })

def format_ingame_clock(clock):
    """Format an in-game clock dict for the status panel.

    '1940-09-06' / '07:23' -> 'Sept 6th 1940, 07:23'
    """
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    date = clock.get('date', '')
    time = clock.get('time', '??:??')
    try:
        year, month, day = (int(x) for x in date.split('-'))
        return f"{months[month - 1]} {day} {year}, {time}"
    except (ValueError, IndexError):
        return time


def parse_radio_message(text):
    """Parse a raw radio message body into display fields.

    Stored format (newline-separated), e.g.:
        From: U-56\nTo: BdU\n27/VIII \nAM 0273 \n Harms
        From: Lehmann Radio News\nTo: on air\n26/VIII \nThe Royal Air Force bombed Berlin yesterday.

    Returns a dict with 'from', 'to', 'date', and 'lines' (the raw split lines).
    """
    lines = [l.rstrip() for l in text.split('\n')]
    fields = {'from': '', 'to': '', 'date': '', 'lines': lines}
    for l in lines:
        s = l.strip()
        if s.startswith('From:'):
            fields['from'] = s[5:].strip()
        elif s.startswith('To:'):
            fields['to'] = s[3:].strip()
        elif '/' in s and len(s) <= 12 and not fields['date']:
            fields['date'] = s
    return fields


def handle_ship_spotted(event):
    """Handle ship spotted events."""
    global last_ship_spotted_time
    
    message = event.data
    last_ship_spotted_time = datetime.now()
    
    add_log_event(f"Ship spotted: {message}", "ship")
    
    if NOTIFY_SHIP_SPOTTED:
        notify.trigger_notification(f"SH3 Alert: {message}")

def handle_aircraft_spotted(event):
    """Handle aircraft spotted events."""
    global last_aircraft_spotted_time
    
    message = event.data
    last_aircraft_spotted_time = datetime.now()
    
    add_log_event(f"Aircraft spotted: {message}", "aircraft")
    
    if NOTIFY_SHIP_SPOTTED:
        notify.trigger_notification(f"SH3 Alert: {message}")

def handle_message_received(event):
    """Handle radio message received events."""
    message = event.data
    timestamp = datetime.now().strftime('%H:%M:%S')
    radio_messages.append({
        'timestamp': timestamp,
        'message': message
    })
    add_log_event(f"Radio message: {message}", "radio")

def handle_weather(event):
    """Handle weather report events (Navigation Officer, Ctrl-W)."""
    global current_weather
    data = event.data
    if isinstance(data, dict):
        current_weather = data
        line = f"[{data.get('date', '?')} {data.get('time', '?')}] {data.get('text', '')}"
    else:
        line = str(data)
    add_log_event(f"Weather: {line}", "status")
def handle_captains_log(event):
    """Handle captain's log entry events."""
    message = event.data
    timestamp = datetime.now().strftime('%H:%M:%S')
    captains_log_entries.append({
        'timestamp': timestamp,
        'message': message
    })
    add_log_event(f"Captain's log: {message.splitlines()[0] if message else message}", "info")

def captains_entry_summary(text):
    """Build a one-line summary for a captain's log entry.

    Uses the first line, plus the second line when present
    (e.g. 'Patrol 9 — U-45, 2nd Flotilla', 'Ship sunk! — SS Foo').
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return '(empty)'
    summary = lines[0]
    if len(lines) > 1:
        summary += f" — {lines[1]}"
    return summary

def handle_location_update(event):
    """Handle location update events (grid + live position)."""
    global current_location, last_grid
    current_location = event.data
    # Log quadrat crossings, but only when the grid comes from the boat's
    # own position (the raw display string transiently holds other
    # entities' grids during radio reports).
    if current_location.get('position') and current_location.get('grid'):
        grid = current_location['grid']
        if last_grid is not None and grid != last_grid:
            add_log_event(f"Grid crossing: {last_grid} -> {grid}", "status")
        last_grid = grid

def handle_clock_update(event):
    """Handle in-game clock update events (from the clock collector)."""
    global current_clock
    current_clock = event.data

def handle_heading_update(event):
    """Handle heading update events."""
    global current_heading
    if isinstance(event.data, dict):
        heading = event.data.get('heading', 0.0)
        current_heading = heading % 360.0

def handle_speed_update(event):
    """Handle speed update events."""
    global current_speed
    
    speed = event.data['speed']
    current_speed = speed
    
    # Only log if there's a warning
    if speed > HIGH_SPEED_THRESHOLD and NOTIFY_HIGH_SPEED:
        msg = f"WARNING: High speed detected: {speed:.2f} kts"
        add_log_event(msg, "warning")
        notify.trigger_notification(f"SH3 Alert: {msg}")

def handle_depth_update(event):
    """Handle depth update events."""
    global current_depth
    
    depth = event.data['depth']
    current_depth = depth

def handle_depth_under_keel_update(event):
    """Handle depth under keel update events."""
    global current_depth_under_keel
    
    depth = event.data['depth']
    current_depth_under_keel = depth
    
    # Only log if there's a warning
    if depth < SHALLOW_DEPTH_THRESHOLD and NOTIFY_SHALLOW_DEPTH:
        msg = f"WARNING: Shallow depth detected: {depth:.2f} m"
        add_log_event(msg, "warning")
        notify.trigger_notification(f"SH3 Alert: {msg}")

def handle_error(event):
    """Handle error events."""
    add_log_event(f"ERROR: {event.data}", "error")
    notify.trigger_notification(f"SH3 Error: {event.data}")

def handle_heartbeat(event):
    """Handle heartbeat events."""
    data = event.data
    log_type = data.get('log_type', 'unknown')
    
    if log_type in heartbeat_time:
        heartbeat_time[log_type] = datetime.now()
    
def handle_search_status(event):
    """Handle search status events."""
    data = event.data
    log_type = data.get('log_type', 'unknown')
    status = data.get('status', 'unknown')
    count = data.get('count', 0)
    
    if log_type in search_status:
        search_status[log_type] = status
        search_log_count[log_type] = count

def handle_status(event):
    """Handle status events."""
    add_log_event(f"STATUS: {event.data}", "info")

def fit(line, width):
    """Truncate/pad a status line to a fixed cell width.

    The status panel uses partial refresh: a line is only rewritten when its
    text changes, and addstr() writes only the given cells. Without padding,
    a shorter value (e.g. 'Last check: 9s ago' after '10s ago') leaves the
    tail of the previous longer line on screen forever.
    """
    return line[:width].ljust(width)

def compute_layout(height, width):
    """Compute the panel geometry for a terminal of the given size.

    Returns a dict with the dimensions used by create_windows(), or None
    when the terminal is too small for the three-column layout.
    """
    radio_width = max(30, min(42, width // 4))
    captains_width = max(32, min(48, width // 4))
    left_width = width - radio_width - captains_width
    log_height = height - STATUS_HEIGHT - FOOTER_HEIGHT
    side_height = STATUS_HEIGHT + log_height
    if height < MIN_HEIGHT or width < MIN_WIDTH:
        return None
    return {
        'status_height': STATUS_HEIGHT,
        'footer_height': FOOTER_HEIGHT,
        'radio_width': radio_width,
        'captains_width': captains_width,
        'left_width': left_width,
        'log_height': log_height,
        'side_height': side_height,
        'width': width,
    }

def create_windows(layout):
    """Create the five panel windows for a valid layout."""
    return {
        'status': curses.newwin(layout['status_height'], layout['left_width'], 0, 0),
        'log': curses.newwin(layout['log_height'], layout['left_width'],
                             layout['status_height'], 0),
        'radio': curses.newwin(layout['side_height'], layout['radio_width'],
                               0, layout['left_width']),
        'captains': curses.newwin(layout['side_height'], layout['captains_width'],
                                  0, layout['left_width'] + layout['radio_width']),
        'footer': curses.newwin(layout['footer_height'], layout['width'],
                                layout['status_height'] + layout['log_height'], 0),
    }

def apply_resize(stdscr, prev_values):
    """Recompute the layout for the current terminal size and rebuild all
    panel windows. Called at startup and on every KEY_RESIZE.

    Why the endwin()/refresh() dance: after a resize the physical screen is
    cleared, but ncurses' internal model (curscr) still believes the old
    content is displayed. doupdate() only emits cells that differ between
    the virtual screen and curscr, so identical static content (panel
    titles, rarely-changing rows, the event log) is never re-emitted and
    stays blank forever. endwin() + stdscr.refresh() invalidates curscr,
    forcing the next doupdate() to repaint the whole screen from scratch.
    (Recreating windows + touchwin() alone is NOT enough - verified
    empirically; see tests/test_ui_resize.py.)

    Windows are rebuilt (not reused) because their geometry changed, and
    prev_values is cleared so draw_status_panel redraws its static elements.

    Returns (windows, layout): a dict of the five panel windows and their
    geometry, or (None, None) when the terminal is too small (a message
    window is shown instead; the next resize recovers automatically).
    """
    # Invalidate ncurses' screen model so the next doupdate() is a full
    # physical repaint instead of a diff against a stale curscr.
    curses.endwin()
    stdscr.refresh()
    try:
        curses.curs_set(0)  # endwin() restores the cursor - hide it again
    except curses.error:
        pass  # not all terminals support cursor visibility control

    height, width = stdscr.getmaxyx()
    layout = compute_layout(height, width)
    if layout is None:
        msg = curses.newwin(height, width, 0, 0)
        try:
            msg.addstr(0, 0, "Terminal too small!", curses.A_REVERSE)
            msg.addstr(1, 0, f"Required: {MIN_HEIGHT} rows x {MIN_WIDTH} columns")
            msg.addstr(2, 0, f"Current:  {height} rows x {width} columns")
            msg.addstr(3, 0, "Resize the terminal to recover (monitor keeps running).")
        except curses.error:
            pass
        msg.noutrefresh()
        return None, None
    windows = create_windows(layout)
    prev_values.clear()
    return windows, layout

def draw_status_panel(status_win, width):
    """Draw the uboat status panel with partial refresh optimization."""
    global previous_values
    
    # Only clear on first draw
    if 'initialized' not in previous_values:
        status_win.clear()
        # Draw static elements (title and separator) once
        status_win.addstr(0, 0, " UBOAT STATUS ", curses.A_REVERSE)
        status_win.hline(14, 0, curses.ACS_HLINE, width)
        previous_values['initialized'] = True
    
    inner = width - 4  # usable width for status lines starting at column 2

    # In-game date/time (from the clock collector)
    if current_clock is None:
        clock_line = " Time:     ---"
    else:
        clock_line = f" Time:     {format_ingame_clock(current_clock)}"
    if previous_values.get('clock_line') != clock_line:
        status_win.addstr(1, 2, fit(clock_line, inner), curses.color_pair(2))
        previous_values['clock_line'] = clock_line

    # Speed
    prev_speed = previous_values.get('speed', -1)
    prev_heading = previous_values.get('heading', -1)
    if (abs(current_speed - prev_speed) > 0.01 or
            abs(current_heading - prev_heading) > 0.5):
        speed_color = curses.color_pair(1)  # Green
        if current_speed > HIGH_SPEED_THRESHOLD:
            speed_color = curses.color_pair(3)  # Yellow
        
        speed_line = f" Speed:  {current_speed:6.2f} kts   Course: {current_heading:03.0f}\u00b0"
        status_win.addstr(2, 2, fit(speed_line, inner), speed_color)
        previous_values['speed'] = current_speed
        previous_values['heading'] = current_heading
    
    # Depth
    prev_depth = previous_values.get('depth', -1)
    if abs(current_depth - prev_depth) > 0.01:  # Only redraw if changed significantly
        depth_color = curses.color_pair(1)  # Green (shallow zone: 0-100m)
        if current_depth >= MEDIUM_DEPTH_THRESHOLD:
            depth_color = curses.color_pair(4)  # Red (deep zone: 170m+)
        elif current_depth >= SHALLOW_DEPTH_THRESHOLD:
            depth_color = curses.color_pair(3)  # Yellow (medium zone: 100-170m)
        
        depth_line = f" Depth:  {current_depth:6.2f} m"
        status_win.addstr(3, 2, fit(depth_line, inner), depth_color)
        previous_values['depth'] = current_depth
    
    # Depth Under Keel
    prev_duk = previous_values.get('depth_under_keel', -1)
    if abs(current_depth_under_keel - prev_duk) > 0.01:  # Only redraw if changed significantly
        depth_under_keel_color = curses.color_pair(1)  # Green
        if current_depth_under_keel < SHALLOW_DEPTH_THRESHOLD:
            depth_under_keel_color = curses.color_pair(3)  # Yellow
        
        depth_under_keel_line = f" Depth Under Keel:  {current_depth_under_keel:6.2f} m"
        status_win.addstr(4, 2, fit(depth_under_keel_line, inner), depth_under_keel_color)
        previous_values['depth_under_keel'] = current_depth_under_keel
    
    # Last ship spotted
    prev_ship_time = previous_values.get('last_ship_spotted_time')
    current_ship_line = None
    if last_ship_spotted_time:
        time_diff = datetime.now() - last_ship_spotted_time
        minutes = int(time_diff.total_seconds() // 60)
        seconds = int(time_diff.total_seconds() % 60)
        current_ship_line = f" Last Ship:  {minutes:2d}m {seconds:2d}s ago"
    else:
        current_ship_line = " Last Ship:  No ships spotted yet"
    
    if prev_ship_time != last_ship_spotted_time or previous_values.get('ship_line') != current_ship_line:
        status_win.addstr(5, 2, fit(current_ship_line, inner), curses.color_pair(2))
        previous_values['last_ship_spotted_time'] = last_ship_spotted_time
        previous_values['ship_line'] = current_ship_line
    
    # Last aircraft spotted
    prev_aircraft_time = previous_values.get('last_aircraft_spotted_time')
    current_aircraft_line = None
    if last_aircraft_spotted_time:
        time_diff = datetime.now() - last_aircraft_spotted_time
        minutes = int(time_diff.total_seconds() // 60)
        seconds = int(time_diff.total_seconds() % 60)
        current_aircraft_line = f" Last Aircraft:  {minutes:2d}m {seconds:2d}s ago"
    else:
        current_aircraft_line = " Last Aircraft:  No aircraft spotted yet"
    
    if prev_aircraft_time != last_aircraft_spotted_time or previous_values.get('aircraft_line') != current_aircraft_line:
        status_win.addstr(6, 2, fit(current_aircraft_line, inner), curses.color_pair(2))
        previous_values['last_aircraft_spotted_time'] = last_aircraft_spotted_time
        previous_values['aircraft_line'] = current_aircraft_line
    
    # Location (naval grid reference + live position from the grid scanner)
    current_loc_line = None
    if current_location is None:
        current_loc_line = " Grid:      ---"
    elif not current_location.get('available', False):
        current_loc_line = " Grid:      unavailable"
    else:
        loc = current_location
        current_loc_line = f" Grid:      {loc['grid']}"
        if loc.get('position'):
            current_loc_line += f"  {loc['lat']:6.2f}N {loc['lon_w']:6.2f}W"
    if previous_values.get('loc_line') != current_loc_line:
        status_win.addstr(7, 2, fit(current_loc_line, inner), curses.color_pair(2))
        previous_values['loc_line'] = current_loc_line

    # Weather (latest Navigation Officer report)
    if current_weather is None:
        current_weather_line = " Weather:   ---"
    else:
        current_weather_line = (f" Weather:   {current_weather.get('time', '?')} "
                                f"{current_weather.get('text', '')}")
    if previous_values.get('weather_line') != current_weather_line:
        status_win.addstr(8, 2, fit(current_weather_line, inner), curses.color_pair(2))
        previous_values['weather_line'] = current_weather_line

    # Heartbeat indicator
    heartbeat_time_ship = heartbeat_time['ship_spotted']
    heartbeat_time_aircraft = heartbeat_time['aircraft_spotted']
    
    # Calculate combined heartbeat status
    ship_active = False
    aircraft_active = False
    
    if heartbeat_time_ship is not None:
        time_diff_ship = datetime.now() - heartbeat_time_ship
        ship_active = time_diff_ship.total_seconds() < 10
    
    if heartbeat_time_aircraft is not None:
        time_diff_aircraft = datetime.now() - heartbeat_time_aircraft
        aircraft_active = time_diff_aircraft.total_seconds() < 10
    
    both_active = ship_active and aircraft_active
    
    if both_active:
        heartbeat_color = curses.color_pair(1)  # Green
        heartbeat_indicator = "●"
    elif ship_active or aircraft_active:
        heartbeat_color = curses.color_pair(3)  # Yellow
        heartbeat_indicator = "○"
    else:
        heartbeat_color = curses.color_pair(4)  # Red
        heartbeat_indicator = "○"
    
    # Last check time (use most recent)
    last_check = "Never"
    if heartbeat_time_ship is not None or heartbeat_time_aircraft is not None:
        time_diffs = []
        if heartbeat_time_ship is not None:
            time_diffs.append(datetime.now() - heartbeat_time_ship)
        if heartbeat_time_aircraft is not None:
            time_diffs.append(datetime.now() - heartbeat_time_aircraft)
        min_diff = min(time_diffs)
        seconds = int(min_diff.total_seconds())
        if seconds < 60:
            last_check = f"{seconds}s ago"
        else:
            minutes = seconds // 60
            last_check = f"{minutes}m {seconds % 60}s ago"
    
    current_heartbeat_line = f" Heartbeat:  {heartbeat_indicator} (Last check: {last_check})"
    if previous_values.get('heartbeat_line') != current_heartbeat_line:
        status_win.addstr(9, 2, fit(current_heartbeat_line, inner), heartbeat_color)
        previous_values['heartbeat_line'] = current_heartbeat_line
    
    # Search Status (ship logs)
    current_ship_search_line = None
    ship_search_status = search_status['ship_spotted']
    if ship_search_status == 'searching':
        current_ship_search_line = " Ship Log Search:  Searching..."
    elif ship_search_status == 'complete':
        count = search_log_count['ship_spotted']
        current_ship_search_line = f" Ship Log Search:  {count} log arrays"
    elif ship_search_status == 'failed':
        current_ship_search_line = " Ship Log Search:  Failed"
    else:
        current_ship_search_line = " Ship Log Search:  Not started"
    
    if previous_values.get('ship_search_status') != ship_search_status or previous_values.get('ship_search_line') != current_ship_search_line:
        ship_search_color = curses.color_pair(3) if ship_search_status == 'searching' else (curses.color_pair(4) if ship_search_status == 'failed' else curses.color_pair(1))
        status_win.addstr(10, 2, fit(current_ship_search_line, inner), ship_search_color)
        previous_values['ship_search_status'] = ship_search_status
        previous_values['ship_search_line'] = current_ship_search_line
    
    # Search Status (aircraft logs)
    current_aircraft_search_line = None
    aircraft_search_status = search_status['aircraft_spotted']
    if aircraft_search_status == 'searching':
        current_aircraft_search_line = " Aircraft Log Search:  Searching..."
    elif aircraft_search_status == 'complete':
        count = search_log_count['aircraft_spotted']
        current_aircraft_search_line = f" Aircraft Log Search:  {count} log arrays"
    elif aircraft_search_status == 'failed':
        current_aircraft_search_line = " Aircraft Log Search:  Failed"
    else:
        current_aircraft_search_line = " Aircraft Log Search:  Not started"
    
    if previous_values.get('aircraft_search_status') != aircraft_search_status or previous_values.get('aircraft_search_line') != current_aircraft_search_line:
        aircraft_search_color = curses.color_pair(3) if aircraft_search_status == 'searching' else (curses.color_pair(4) if aircraft_search_status == 'failed' else curses.color_pair(1))
        status_win.addstr(11, 2, fit(current_aircraft_search_line, inner), aircraft_search_color)
        previous_values['aircraft_search_status'] = aircraft_search_status
        previous_values['aircraft_search_line'] = current_aircraft_search_line

    # Search Status (radio messages)
    current_radio_search_line = None
    radio_search_status = search_status['message_received']
    if radio_search_status == 'searching':
        current_radio_search_line = " Radio Msg Search:  Searching..."
    elif radio_search_status == 'complete':
        count = search_log_count['message_received']
        current_radio_search_line = f" Radio Msg Search:  {count} messages"
    elif radio_search_status == 'failed':
        current_radio_search_line = " Radio Msg Search:  Failed"
    else:
        current_radio_search_line = " Radio Msg Search:  Not started"
    if previous_values.get('radio_search_status') != radio_search_status or \
       previous_values.get('radio_search_line') != current_radio_search_line:
        rcolor = curses.color_pair(3) if radio_search_status == 'searching' else \
                 (curses.color_pair(4) if radio_search_status == 'failed' else curses.color_pair(1))
        status_win.addstr(12, 2, fit(current_radio_search_line, inner), rcolor)
        previous_values['radio_search_status'] = radio_search_status
        previous_values['radio_search_line'] = current_radio_search_line

    # Search Status (captain's log)
    current_captains_search_line = None
    captains_search_status = search_status['captains_log']
    if captains_search_status == 'searching':
        current_captains_search_line = " Capt. Log Search:  Searching..."
    elif captains_search_status == 'complete':
        count = search_log_count['captains_log']
        current_captains_search_line = f" Capt. Log Search:  {count} entries"
    elif captains_search_status == 'failed':
        current_captains_search_line = " Capt. Log Search:  Failed"
    else:
        current_captains_search_line = " Capt. Log Search:  Not started"
    if previous_values.get('captains_search_status') != captains_search_status or \
       previous_values.get('captains_search_line') != current_captains_search_line:
        ccolor = curses.color_pair(3) if captains_search_status == 'searching' else \
                 (curses.color_pair(4) if captains_search_status == 'failed' else curses.color_pair(1))
        status_win.addstr(13, 2, fit(current_captains_search_line, inner), ccolor)
        previous_values['captains_search_status'] = captains_search_status
        previous_values['captains_search_line'] = current_captains_search_line

    # Refresh the window
    status_win.noutrefresh()

def draw_log_panel(log_win, height, width):
    """Draw the event log panel."""
    # Clear the window
    log_win.erase()  # erase, not clear: clear() sets clearok and forces a
                      # full-screen clear+repaint every frame
    
    # Panel title (simplified - no quit instruction here)
    log_win.addstr(0, 0, " EVENT LOG ", curses.A_REVERSE)
    
    # Calculate available space for log
    log_height = height - 1
    log_width = width - 2
    
    # Show most recent events first (reverse order)
    visible_events = list(reversed(event_log))[:log_height]
    
    for i, event in enumerate(visible_events):
        row = 1 + i
        
        # Determine color based on event type
        color = curses.color_pair(1)  # Green (default)
        if event['type'] == 'ship':
            color = curses.color_pair(2)  # Blue
        elif event['type'] == 'aircraft':
            color = curses.color_pair(2)  # Blue (same as ship)
        elif event['type'] == 'warning':
            color = curses.color_pair(3)  # Yellow
        elif event['type'] == 'critical':
            color = curses.color_pair(4)  # Red
        elif event['type'] == 'error':
            color = curses.color_pair(4)  # Red
        
        # Format log line
        log_line = f"[{event['timestamp']}] {event['message']}"
        
        # Truncate if too long
        if len(log_line) > log_width:
            log_line = log_line[:log_width-3] + "..."
        
        log_win.addstr(row, 1, log_line, color)
    
    # Refresh the window
    log_win.noutrefresh()

def draw_radio_panel(radio_win, height, width, focused):
    """Draw the radio messages list panel on the right side."""
    global radio_selected, radio_scroll
    radio_win.erase()  # erase, not clear: avoid clearok full-screen repaints
    radio_win.border()

    # Title
    title = " RADIO MESSAGES "
    if focused:
        title = " RADIO MESSAGES [FOCUSED] "
    title = title[:width - 2]
    radio_win.addstr(0, 1, title, curses.A_REVERSE if focused else curses.color_pair(2))

    # Message count (top-right of title row)
    count_text = f" {len(radio_messages)} msgs "
    radio_win.addstr(0, width - len(count_text) - 1, count_text, curses.color_pair(1))

    # List area dimensions (inside border)
    list_height = height - 2
    list_width = width - 4

    messages = list(radio_messages)
    if not messages:
        radio_win.addstr(2, 2, "(no messages yet)", curses.color_pair(3))
        radio_win.noutrefresh()
        return

    # Clamp selection
    if radio_selected >= len(messages):
        radio_selected = len(messages) - 1
    if radio_selected < 0:
        radio_selected = 0

    # Adjust scroll so the selected item stays visible
    if radio_scroll > radio_selected:
        radio_scroll = radio_selected
    if radio_scroll < radio_selected - list_height + 1:
        radio_scroll = radio_selected - list_height + 1
    if radio_scroll < 0:
        radio_scroll = 0

    for i in range(list_height):
        idx = radio_scroll + i
        if idx >= len(messages):
            break
        msg = messages[idx]
        f = parse_radio_message(msg['message'])
        summary = f"{f['from']} -> {f['to']}  {f['date']}".strip()
        if len(summary) > list_width:
            summary = summary[:list_width - 3] + "..."
        row = 1 + i
        if idx == radio_selected:
            attr = curses.A_REVERSE if focused else curses.color_pair(3)
            radio_win.addstr(row, 1, summary.ljust(list_width), attr)
        else:
            radio_win.addstr(row, 1, summary, curses.color_pair(1))

    # Scroll indicator
    if len(messages) > list_height:
        indicator = f"[{radio_scroll + 1}-{min(radio_scroll + list_height, len(messages))}/{len(messages)}]"
        radio_win.addstr(height - 1, width - len(indicator) - 1, indicator, curses.color_pair(3))

    radio_win.noutrefresh()


def draw_captains_panel(captains_win, height, width, focused):
    """Draw the captain's log entries panel (rightmost column)."""
    global captains_selected, captains_scroll
    captains_win.erase()  # erase, not clear: avoid clearok full-screen repaints
    captains_win.border()

    # Title
    title = " CAPTAIN'S LOG "
    if focused:
        title = " CAPTAIN'S LOG [FOCUSED] "
    title = title[:width - 2]
    captains_win.addstr(0, 1, title, curses.A_REVERSE if focused else curses.color_pair(2))

    # Entry count (top-right of title row)
    count_text = f" {len(captains_log_entries)} entries "
    captains_win.addstr(0, width - len(count_text) - 1, count_text, curses.color_pair(1))

    # List area dimensions (inside border)
    list_height = height - 2
    list_width = width - 4

    entries = list(captains_log_entries)
    if not entries:
        captains_win.addstr(2, 2, "(no entries yet)", curses.color_pair(3))
        captains_win.noutrefresh()
        return

    # Clamp selection
    if captains_selected >= len(entries):
        captains_selected = len(entries) - 1
    if captains_selected < 0:
        captains_selected = 0

    # Adjust scroll so the selected item stays visible
    if captains_scroll > captains_selected:
        captains_scroll = captains_selected
    if captains_scroll < captains_selected - list_height + 1:
        captains_scroll = captains_selected - list_height + 1
    if captains_scroll < 0:
        captains_scroll = 0

    for i in range(list_height):
        idx = captains_scroll + i
        if idx >= len(entries):
            break
        entry = entries[idx]
        summary = captains_entry_summary(entry['message'])
        if len(summary) > list_width:
            summary = summary[:list_width - 3] + "..."
        row = 1 + i
        if idx == captains_selected:
            attr = curses.A_REVERSE if focused else curses.color_pair(3)
            captains_win.addstr(row, 1, summary.ljust(list_width), attr)
        else:
            captains_win.addstr(row, 1, summary, curses.color_pair(1))

    # Scroll indicator
    if len(entries) > list_height:
        indicator = f"[{captains_scroll + 1}-{min(captains_scroll + list_height, len(entries))}/{len(entries)}]"
        captains_win.addstr(height - 1, width - len(indicator) - 1, indicator, curses.color_pair(3))

    captains_win.noutrefresh()


def draw_detail_popup(stdscr, title, subtitle, lines):
    """Draw a centered overlay popup showing full text lines."""
    h, w = stdscr.getmaxyx()

    # Popup dimensions
    box_w = min(w - 4, 72)
    box_h = min(h - 4, len(lines) + 6)
    box_h = max(box_h, 8)
    y0 = (h - box_h) // 2
    x0 = (w - box_w) // 2

    popup = curses.newwin(box_h, box_w, y0, x0)
    popup.border()
    popup.addstr(0, 1, f" {title} ", curses.A_REVERSE)
    popup.addstr(1, 1, subtitle, curses.color_pair(2))

    inner_w = box_w - 4
    row = 3
    for line in lines:
        s = line.rstrip()
        while s and row < box_h - 1:
            chunk = s[:inner_w]
            popup.addstr(row, 2, chunk)
            s = s[inner_w:]
            row += 1
        if row >= box_h - 1:
            break
        if not s:
            row += 1 if row < box_h - 1 else 0

    popup.addstr(box_h - 1, 2, " Esc/Enter: close ", curses.color_pair(3))
    popup.noutrefresh()


def draw_radio_detail(stdscr, msg):
    """Draw a centered overlay popup showing the full radio message text."""
    f = parse_radio_message(msg['message'])
    draw_detail_popup(stdscr, "RADIO MESSAGE", f"Received: {msg['timestamp']}", f['lines'])


def draw_captains_detail(stdscr, entry):
    """Draw a centered overlay popup showing the full captain's log entry."""
    lines = [l.rstrip() for l in entry['message'].split('\n')]
    draw_detail_popup(stdscr, "CAPTAIN'S LOG", f"Logged: {entry['timestamp']}", lines)


# ---------------------------------------------------------------------------
# Crew screen (story #13): SH3-style crew management view, side cutaway
#
# The cutaway is a fixed 74x12 character grid. Rows 0-1 are the flak
# platform, rows 2-4 the conning tower (bridge) and the deck gun, row 5 the
# deck line, rows 6-7 compartment labels, rows 8-9 crew glyph slots, row 10
# the keel. Crew members are drawn as single glyphs at per-station slot
# coordinates, color-coded by fatigue.
# ---------------------------------------------------------------------------

CUTAWAY_WIDTH = 74
CUTAWAY_HEIGHT = 12

# Hull compartments: (id, label line 1, label line 2, x_start, x_end)
COMPARTMENTS = [
    ('bow_torpedo',       'BOW TORP.', '',        2,  12),
    ('crew_quarters',     'CREW QTRS', '(bunks)', 14, 24),
    ('radio_room',        'RADIO',     '',        26, 31),
    ('control_room',      'CONTROL',   'ZENTRAL', 33, 44),
    ('officers_quarters', 'OFFIC.',    'QTRS',    46, 52),
    ('engine_room',       'ENGINE',    '',        54, 62),
    ('stern_torpedo',     'STERN',     'TORP.',   64, 71),
]

# Crew glyph slot coordinates (y, x) per station, inside the cutaway grid.
# Hull stations have two rows of slots; tower/deck stations one.
STATION_SLOTS = {
    'bow_torpedo':       [(8, 3), (8, 6), (8, 9), (8, 12), (9, 4), (9, 7), (9, 10)],
    'crew_quarters':     [(8, 15), (8, 18), (8, 21), (8, 24), (9, 16), (9, 19), (9, 22)],
    'radio_room':        [(8, 27), (8, 30), (9, 28)],
    'control_room':      [(8, 34), (8, 37), (8, 40), (8, 43), (9, 35), (9, 38), (9, 41)],
    'officers_quarters': [(8, 47), (8, 50), (9, 48), (9, 51)],
    'engine_room':       [(8, 55), (8, 58), (8, 61), (9, 56), (9, 59)],
    'stern_torpedo':     [(8, 65), (8, 68), (8, 71), (9, 66), (9, 69)],
    'bridge':            [(3, 36), (3, 38), (3, 40), (3, 43), (3, 45)],
    'flak':              [(1, 44), (1, 46)],
    'deck_gun':          [(4, 12), (4, 14)],
}

# Officer/petty-officer specialties get a role letter; everyone else is 'o'.
ROLE_LETTERS = {
    'watch': 'W', 'engineer': 'L', 'navigator': 'N', 'radio': 'R',
    'sonar': 'S', 'torpedo': 'T', 'gunner': 'G', 'medic': 'M',
}

STATION_LABELS = {
    'bow_torpedo': 'Bow Torp', 'crew_quarters': 'Crew Qtrs',
    'radio_room': 'Radio', 'control_room': 'Control',
    'officers_quarters': 'Off Qtrs', 'engine_room': 'Engine',
    'stern_torpedo': 'Stern Torp', 'bridge': 'Bridge',
    'flak': 'Flak', 'deck_gun': 'Deck Gun',
}


def build_cutaway():
    """Build the static side-cutaway art of the U-boat (list of 12 strings,
    each CUTAWAY_WIDTH characters). Crew glyphs are drawn on top of this."""
    rows = [' ' * CUTAWAY_WIDTH for _ in range(CUTAWAY_HEIGHT)]

    def put(y, x, s):
        rows[y] = rows[y][:x] + s + rows[y][x + len(s):]

    # Conning tower: flak platform on top, bridge below
    put(0, 42, '\u250cFLAK\u2510')
    put(1, 42, '\u2502')
    put(1, 47, '\u2502')
    put(2, 34, '\u250c\u2500BRIDGE\u2534\u2500\u2500\u2500\u2500\u2518')
    put(3, 34, '\u2502')
    put(3, 47, '\u2502')
    put(4, 34, '\u2502')
    put(4, 47, '\u2502')

    # Deck gun, forward of the tower
    put(4, 8, '\u2584\u2588\u2584')

    # Hull: deck line, keel, side walls
    put(5, 1, '\u2554' + '\u2550' * 70 + '\u2557')
    put(10, 1, '\u255a' + '\u2550' * 70 + '\u255d')
    for y in range(6, 10):
        put(y, 1, '\u2551')
        put(y, 72, '\u2551')

    # Tower meets the deck
    put(5, 34, '\u2567')
    put(5, 47, '\u2567')

    # Compartment separators and labels
    for _cid, l1, l2, x0, _x1 in COMPARTMENTS:
        if x0 > 2:
            for y in range(6, 10):
                put(y, x0 - 1, '\u2551')
        if l1:
            put(6, x0, l1)
        if l2:
            put(7, x0, l2)
    return rows


def demo_crew():
    """Return a plausible Type VIIC roster for UI testing.

    Fallback for trying the crew screen without the game running; the
    screen marks it as DEMO data. Live rosters arrive via CREW_UPDATE
    events from the crew collector and replace it automatically.
    """
    return [
        # officers on duty
        {'name': 'F. Hansen',  'rank': 'Oblt.z.S.',  'specialty': 'watch',     'station': 'bridge',       'on_duty': True,  'fatigue': 0.32, 'officer': True},
        {'name': 'K. Weber',   'rank': 'Oblt.(Ing.)', 'specialty': 'engineer',  'station': 'control_room',  'on_duty': True,  'fatigue': 0.55, 'officer': True},
        {'name': 'J. Brandt',  'rank': 'Lt.z.S.',    'specialty': 'navigator', 'station': 'control_room',  'on_duty': True,  'fatigue': 0.28, 'officer': True},
        {'name': 'O. Schmitt', 'rank': 'Lt.z.S.',    'specialty': 'radio',     'station': 'radio_room',   'on_duty': True,  'fatigue': 0.41, 'officer': True},
        {'name': 'H. Vogel',   'rank': 'Lt.z.S.',    'specialty': 'sonar',     'station': 'control_room',  'on_duty': True,  'fatigue': 0.66, 'officer': True},
        # enlisted on duty
        {'name': 'P. Klein',   'rank': 'Bootsmaat',    'specialty': 'torpedo', 'station': 'bow_torpedo',   'on_duty': True,  'fatigue': 0.71},
        {'name': 'W. Roth',    'rank': 'Matrose',      'specialty': None,      'station': 'bow_torpedo',   'on_duty': True,  'fatigue': 0.48},
        {'name': 'E. Falk',    'rank': 'Matrose',      'specialty': None,      'station': 'bow_torpedo',   'on_duty': True,  'fatigue': 0.39},
        {'name': 'G. Bauer',   'rank': 'Maschinenmaat', 'specialty': None,    'station': 'engine_room',   'on_duty': True,  'fatigue': 0.83},
        {'name': 'L. Horn',    'rank': 'Maschinenmaat', 'specialty': None,    'station': 'engine_room',   'on_duty': True,  'fatigue': 0.57},
        {'name': 'T. Seidel',  'rank': 'Matrose',      'specialty': None,      'station': 'engine_room',   'on_duty': True,  'fatigue': 0.44},
        {'name': 'R. Vogt',    'rank': 'Funkmaat',     'specialty': None,      'station': 'radio_room',   'on_duty': True,  'fatigue': 0.36},
        {'name': 'A. J\u00e4ger',   'rank': 'Maat',        'specialty': None,      'station': 'control_room',  'on_duty': True,  'fatigue': 0.62},
        {'name': 'M. Lorenz',  'rank': 'Matrose',      'specialty': None,      'station': 'control_room',  'on_duty': True,  'fatigue': 0.51},
        {'name': 'H. Baum',    'rank': 'Matrose',      'specialty': 'gunner',  'station': 'flak',          'on_duty': True,  'fatigue': 0.77},
        {'name': 'F. Sander',  'rank': 'Matrose',      'specialty': 'gunner',  'station': 'flak',          'on_duty': True,  'fatigue': 0.69},
        {'name': 'K. Neumann', 'rank': 'Matrose',      'specialty': 'gunner',  'station': 'deck_gun',      'on_duty': True,  'fatigue': 0.35},
        {'name': 'U. Walter',  'rank': 'Matrose',      'specialty': None,      'station': 'deck_gun',      'on_duty': True,  'fatigue': 0.29},
        {'name': 'D. Kr\u00fcger',  'rank': 'Bootsmaat',    'specialty': 'torpedo', 'station': 'stern_torpedo', 'on_duty': True,  'fatigue': 0.58},
        {'name': 'S. Otto',    'rank': 'Matrose',      'specialty': None,      'station': 'stern_torpedo', 'on_duty': True,  'fatigue': 0.47},
        {'name': 'B. Vo\u00df',     'rank': 'Matrose',      'specialty': None,      'station': 'bridge',       'on_duty': True,  'fatigue': 0.74},
        {'name': 'C. Marx',    'rank': 'Matrose',      'specialty': None,      'station': 'bridge',       'on_duty': True,  'fatigue': 0.52},
        # off duty, resting in bunks
        {'name': 'I. Lang',    'rank': 'Matrose',      'specialty': None,      'station': 'crew_quarters', 'on_duty': False, 'fatigue': 0.18},
        {'name': 'N. Braun',   'rank': 'Matrose',      'specialty': None,      'station': 'crew_quarters', 'on_duty': False, 'fatigue': 0.12},
        {'name': 'O. Heim',    'rank': 'Maschinenmaat', 'specialty': None,    'station': 'crew_quarters', 'on_duty': False, 'fatigue': 0.25},
        {'name': 'V. Sattler', 'rank': 'Matrose',      'specialty': 'medic',  'station': 'crew_quarters', 'on_duty': False, 'fatigue': 0.09},
        {'name': 'Z. Frank',   'rank': 'Matrose',      'specialty': None,      'station': 'crew_quarters', 'on_duty': False, 'fatigue': 0.31},
        {'name': 'Q. Adler',   'rank': 'Lt.z.S.',      'specialty': None,      'station': 'officers_quarters', 'on_duty': False, 'fatigue': 0.22, 'officer': True},
    ]


def handle_crew_update(event):
    """Handle crew state update events (from the crew collector, #12)."""
    global crew_state, crew_demo
    data = event.data
    crew = data.get('crew') if isinstance(data, dict) else None
    if isinstance(crew, list) and crew:
        crew_state = crew
        crew_demo = False


def toggle_crew_demo():
    """Load/remove the demo roster on the crew screen (until #12 lands)."""
    global crew_state, crew_demo, crew_selected, crew_scroll
    if crew_demo:
        crew_state = None
        crew_demo = False
        add_log_event("Crew demo data removed", "info")
    else:
        crew_state = demo_crew()
        crew_demo = True
        crew_selected = 0
        crew_scroll = 0
        add_log_event("Crew demo data loaded (no live CREW_UPDATE data)", "info")


def fatigue_color_pair(fatigue):
    """Color pair index for a fatigue value: 1 green / 3 yellow / 4 red."""
    if fatigue >= CREW_FATIGUE_HIGH:
        return 4
    if fatigue >= CREW_FATIGUE_MEDIUM:
        return 3
    return 1


def crew_glyph(member):
    """The cutaway glyph for a crew member: role letter, 'o', or 'z' (bunk)."""
    if not member.get('on_duty', True):
        return 'z'
    return ROLE_LETTERS.get(member.get('specialty'), 'o')


def member_station(member):
    """The station a member is displayed at (off-duty crew shown in bunks)."""
    if member.get('on_duty', True):
        return member.get('station')
    return 'officers_quarters' if member.get('officer') else 'crew_quarters'


def crew_glyph_positions(members):
    """Compute cutaway glyph positions for a crew list.

    Returns a list of (y, x, char, color_pair_index, bold, dim) tuples.
    Members fill their station's slots in list order; when a station has
    more members than slots a yellow '+' marks the overflow.
    """
    positions = []
    overflow = {}
    by_station = {}
    for m in members:
        by_station.setdefault(member_station(m), []).append(m)
    for st, group in by_station.items():
        slots = STATION_SLOTS.get(st, [])
        for i, m in enumerate(group):
            if i < len(slots):
                y, x = slots[i]
                f = m.get('fatigue', 0.0)
                positions.append((y, x, crew_glyph(m),
                                  fatigue_color_pair(f),
                                  m.get('specialty') in ROLE_LETTERS,
                                  not m.get('on_duty', True)))
            else:
                overflow[st] = overflow.get(st, 0) + 1
    # Overflow markers: right edge of the second slot row for hull
    # compartments, just past the last slot for tower/deck stations.
    xends = {cid: x1 for cid, _l1, _l2, _x0, x1 in COMPARTMENTS}
    for st in overflow:
        slots = STATION_SLOTS.get(st, [])
        if not slots:
            continue
        if st in xends:
            my, mx = 9, xends[st]
        else:
            my, mx = slots[-1][0], slots[-1][1] + 2
        positions.append((my, mx, '+', 3, False, False))
    return positions


def crew_summary(members):
    """One-line crew summary for under the cutaway."""
    on_duty = [m for m in members if m.get('on_duty', True)]
    avg = sum(m.get('fatigue', 0.0) for m in members) / len(members)
    worst = max(members, key=lambda m: m.get('fatigue', 0.0))
    return (f"{len(members)} aboard \u00b7 {len(on_duty)} on duty \u00b7 "
            f"avg fatigue {avg * 100:.0f}% \u00b7 worst: {worst['name']} "
            f"({worst.get('fatigue', 0.0) * 100:.0f}%)")


def fatigue_bar(fatigue, width):
    """A fatigue bar, e.g. width 6 at 50% -> '\u2588\u2588\u2588\u2591\u2591\u2591'."""
    filled = max(0, min(width, round(fatigue * width)))
    return '\u2588' * filled + '\u2591' * (width - filled)


def format_roster_row(idx, member, name_w, station_w, bar_w):
    """Format one roster row (pure function; truncates to the given widths)."""
    name = member.get('name', '?')
    if member.get('on_duty', True):
        st = STATION_LABELS.get(member.get('station'), member.get('station') or '?')
    else:
        st = 'Bunk'
    f = member.get('fatigue', 0.0)
    row = f"{idx:2d} {name:<{name_w}.{name_w}}"
    if station_w:
        row += f" {st:<{station_w}.{station_w}}"
    row += f" {fatigue_bar(f, bar_w)} {f * 100:3.0f}%"
    return row


def draw_crew_roster(roster_win, height, width, members):
    """Draw the scrollable crew roster panel (right side of the crew screen)."""
    global crew_selected, crew_scroll
    roster_win.erase()
    roster_win.border()

    title = " CREW ROSTER "
    roster_win.addstr(0, 1, title, curses.color_pair(2))
    count_text = f" {len(members)} "
    roster_win.addstr(0, width - len(count_text) - 1, count_text, curses.color_pair(1))

    list_height = height - 2
    inner = width - 2

    if not members:
        roster_win.addstr(2, 2, "(no crew data)", curses.color_pair(3))
        roster_win.addstr(3, 2, "d: demo roster", curses.color_pair(3))
        roster_win.noutrefresh()
        return

    # Clamp selection
    if crew_selected >= len(members):
        crew_selected = len(members) - 1
    if crew_selected < 0:
        crew_selected = 0

    # Adjust scroll so the selected item stays visible
    if crew_scroll > crew_selected:
        crew_scroll = crew_selected
    if crew_scroll < crew_selected - list_height + 1:
        crew_scroll = crew_selected - list_height + 1
    if crew_scroll < 0:
        crew_scroll = 0

    # Adaptive columns: idx(2) sp name sp [station sp] bar pct(4)
    bar_w = 6 if inner < 34 else 8
    name_w = min(18, inner - bar_w - 9)
    if name_w < 4:
        name_w = 4
    station_w = 10 if inner - bar_w - 9 - name_w >= 11 else 0

    # Column header
    header = f"{'#':>2} {'Name':<{name_w}.{name_w}}"
    if station_w:
        header += f" {'Station':<{station_w}.{station_w}}"
    header += f" {'Fatig.':>{bar_w + 5}}"
    roster_win.addstr(1, 1, header[:inner], curses.color_pair(2))

    for i in range(list_height - 1):  # -1 for the header row
        idx = crew_scroll + i
        if idx >= len(members):
            break
        m = members[idx]
        row = format_roster_row(idx + 1, m, name_w, station_w, bar_w)
        row_y = 2 + i
        if idx == crew_selected:
            roster_win.addstr(row_y, 1, row[:inner].ljust(inner), curses.A_REVERSE)
        else:
            roster_win.addstr(row_y, 1, row[:inner],
                              curses.color_pair(fatigue_color_pair(m.get('fatigue', 0.0))))

    if len(members) > list_height - 1:
        indicator = f"[{crew_scroll + 1}-{min(crew_scroll + list_height - 1, len(members))}/{len(members)}]"
        roster_win.addstr(height - 1, width - len(indicator) - 1, indicator, curses.color_pair(3))

    roster_win.noutrefresh()


def draw_crew_screen(stdscr):
    """Draw the full-screen crew management overlay (story #13).

    Covers all panels except the footer row, which keeps showing the
    crew-screen keybindings.
    """
    height, width = stdscr.getmaxyx()
    win = curses.newwin(max(height - 1, 1), width, 0, 0)
    win.erase()

    title = " CREW MANAGEMENT "
    if crew_demo:
        title += "- DEMO DATA "
    try:
        win.addstr(0, 0, title[:width - 1], curses.A_REVERSE)
    except curses.error:
        pass

    members = list(crew_state) if crew_state else []

    # Side cutaway with crew glyphs
    art = build_cutaway()
    for i, line in enumerate(art):
        if 2 + i >= height - 1:
            break
        try:
            win.addstr(2 + i, 1, line, curses.color_pair(2))
        except curses.error:
            pass
    for (y, x, ch, cpair, bold, dim) in crew_glyph_positions(members):
        if 2 + y >= height - 1:
            continue
        attr = curses.color_pair(cpair)
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        try:
            win.addstr(2 + y, 1 + x, ch, attr)
        except curses.error:
            pass

    # Summary and legend under the cutaway
    row = 2 + CUTAWAY_HEIGHT + 1
    if row < height - 1:
        if members:
            try:
                win.addstr(row, 1, crew_summary(members)[:width - 2], curses.color_pair(2))
            except curses.error:
                pass
        else:
            try:
                win.addstr(row, 1, " No crew data yet - press d for a demo roster",
                           curses.color_pair(3))
            except curses.error:
                pass
    row += 1
    if row < height - 1:
        try:
            win.addstr(row, 1, " W watch L LI N nav R radio S sonar T torp G gun M medic o crew z bunk",
                       curses.color_pair(2))
        except curses.error:
            pass
    row += 1
    if row < height - 1:
        try:
            win.addstr(row, 1, " fatigue: green fit / yellow tired / red exhausted",
                       curses.color_pair(2))
        except curses.error:
            pass

    win.noutrefresh()

    # Roster panel on the right (needs the remaining columns)
    roster_x = 1 + CUTAWAY_WIDTH + 1
    roster_w = width - roster_x
    if roster_w >= 24 and height > 5:
        roster_h = height - 1 - 2
        roster = curses.newwin(roster_h, roster_w, 2, roster_x)
        try:
            draw_crew_roster(roster, roster_h, roster_w, members)
        except curses.error:
            pass


def draw_crew_detail(stdscr, member):
    """Draw a centered overlay popup with one crew member's details."""
    if member.get('on_duty', True):
        st = STATION_LABELS.get(member.get('station'), member.get('station') or '?')
        duty = 'on duty'
    else:
        st = 'Bunk'
        duty = 'off duty, resting'
    lines = [
        f"Name:      {member.get('name', '?')}",
        f"Rank:      {member.get('rank', '?')}",
        f"Specialty: {member.get('specialty') or '-'}",
        f"Station:   {st} ({duty})",
        f"Fatigue:   {member.get('fatigue', 0.0) * 100:.0f}%",
    ]
    draw_detail_popup(stdscr, "CREW MEMBER", member.get('rank', ''), lines)


def draw_footer(footer_win, width):
    """Draw the footer with context-sensitive keybindings."""
    footer_win.erase()  # erase, not clear: avoid clearok full-screen repaints

    if radio_detail_open:
        text = " Enter/Esc: close message "
    elif captains_detail_open:
        text = " Enter/Esc: close entry "
    elif crew_detail_open:
        text = " Enter/Esc: close crewman "
    elif crew_screen_open:
        text = " Up/Dn: select   Enter: details   d: demo data   c/Esc: close crew screen "
    elif panel_focus == FOCUS_RADIO:
        text = " Up/Dn: navigate   Enter: open   Esc: unfocus   Tab: switch   q: quit "
    elif panel_focus == FOCUS_CAPTAINS:
        text = " Up/Dn: navigate   Enter: open   Esc: unfocus   Tab: switch   q: quit "
    else:
        text = " Tab: cycle panels   c: crew screen   q: quit "

    text = text[:width]
    start_col = max(0, (width - len(text)) // 2)
    footer_win.addstr(0, start_col, text, curses.A_REVERSE)

    footer_win.noutrefresh()

def main_loop(stdscr):
    """Main ncurses event loop."""
    global running, panel_focus, radio_selected, radio_scroll, radio_detail_open
    global captains_selected, captains_scroll, captains_detail_open
    global crew_screen_open, crew_detail_open, crew_selected, crew_scroll
    
    # Initialize colors
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_RED, curses.COLOR_BLACK)
    
    # Setup curses
    curses.curs_set(0)  # Hide cursor
    stdscr.nodelay(1)  # Non-blocking input
    stdscr.timeout(int(REFRESH_RATE * 1000))  # Timeout in milliseconds
    
    # Panel windows and geometry. Built by relayout() at startup and rebuilt
    # on every terminal resize (KEY_RESIZE) - see apply_resize() for why the
    # windows must be recreated rather than reused. While the terminal is
    # too small, the window handles are None and only a message is shown.
    status_win = log_win = radio_win = captains_win = footer_win = None
    log_height = left_width = side_height = radio_width = captains_width = 0

    # Track if log needs redrawing
    last_log_count = 0
    last_radio_count = 0
    last_captains_count = 0
    prev_detail_open = False

    def relayout():
        """Rebuild the panel layout for the current terminal size.

        Returns True when the full layout is active, False when the terminal
        is too small (the monitor keeps running and recovers on the next
        resize).
        """
        nonlocal status_win, log_win, radio_win, captains_win, footer_win
        nonlocal log_height, left_width, side_height, radio_width, captains_width
        nonlocal last_log_count, last_radio_count, last_captains_count
        windows, layout = apply_resize(stdscr, previous_values)
        if windows is None:
            status_win = log_win = radio_win = captains_win = footer_win = None
            return False
        status_win = windows['status']
        log_win = windows['log']
        radio_win = windows['radio']
        captains_win = windows['captains']
        footer_win = windows['footer']
        log_height = layout['log_height']
        left_width = layout['left_width']
        side_height = layout['side_height']
        radio_width = layout['radio_width']
        captains_width = layout['captains_width']
        # Force the change-detected panels to redraw into the fresh windows
        last_log_count = last_radio_count = last_captains_count = -1
        return True

    relayout()

    while running:
        # Handle keyboard input
        key = stdscr.getch()

        # Terminal resized (tmux client attach/detach, window resize, ...):
        # rebuild all panel windows and force a full repaint. Without this,
        # every static element stays blank after a resize.
        if key == curses.KEY_RESIZE:
            relayout()

        # Detail overlays are modal: intercept close keys before anything else
        if radio_detail_open:
            if key == 27 or key == ord('\n') or key == curses.KEY_ENTER or key == ord('q') or key == ord('Q'):
                radio_detail_open = False
        elif captains_detail_open:
            if key == 27 or key == ord('\n') or key == curses.KEY_ENTER or key == ord('q') or key == ord('Q'):
                captains_detail_open = False
        elif crew_detail_open:
            if key == 27 or key == ord('\n') or key == curses.KEY_ENTER or key == ord('q') or key == ord('Q'):
                crew_detail_open = False
        elif crew_screen_open:
            # Crew screen is modal: its keys take precedence over the panels
            if key == 27 or key == ord('c') or key == ord('C') or key == ord('q') or key == ord('Q'):
                crew_screen_open = False
                crew_detail_open = False
            elif key == curses.KEY_UP:
                crew_selected = max(0, crew_selected - 1)
            elif key == curses.KEY_DOWN:
                if crew_state:
                    crew_selected = min(len(crew_state) - 1, crew_selected + 1)
            elif key == curses.KEY_PPAGE:
                crew_selected = max(0, crew_selected - 10)
            elif key == curses.KEY_NPAGE:
                if crew_state:
                    crew_selected = min(len(crew_state) - 1, crew_selected + 10)
            elif key == curses.KEY_HOME:
                crew_selected = 0
            elif key == curses.KEY_END:
                if crew_state:
                    crew_selected = len(crew_state) - 1
            elif key == ord('\n') or key == curses.KEY_ENTER:
                if crew_state:
                    crew_detail_open = True
            elif key == ord('d') or key == ord('D'):
                toggle_crew_demo()
        elif key == ord('q') or key == ord('Q'):
            running = False
            break
        elif key == ord('\t'):
            # Cycle focus: none -> radio -> captain's log -> none
            panel_focus = (panel_focus + 1) % 3
            radio_detail_open = False
            captains_detail_open = False
        elif key == ord('c') or key == ord('C'):
            # Open the crew screen overlay (story #13)
            crew_screen_open = True
            crew_detail_open = False
            crew_selected = 0
            crew_scroll = 0
        elif panel_focus == FOCUS_RADIO:
            if key == curses.KEY_UP:
                radio_selected = max(0, radio_selected - 1)
            elif key == curses.KEY_DOWN:
                if radio_messages:
                    radio_selected = min(len(radio_messages) - 1, radio_selected + 1)
            elif key == curses.KEY_PPAGE:
                radio_selected = max(0, radio_selected - 10)
            elif key == curses.KEY_NPAGE:
                if radio_messages:
                    radio_selected = min(len(radio_messages) - 1, radio_selected + 10)
            elif key == curses.KEY_HOME:
                radio_selected = 0
            elif key == curses.KEY_END:
                if radio_messages:
                    radio_selected = len(radio_messages) - 1
            elif key == ord('\n') or key == curses.KEY_ENTER:
                if radio_messages:
                    radio_detail_open = True
            elif key == 27:  # Esc - unfocus
                panel_focus = FOCUS_NONE
        elif panel_focus == FOCUS_CAPTAINS:
            if key == curses.KEY_UP:
                captains_selected = max(0, captains_selected - 1)
            elif key == curses.KEY_DOWN:
                if captains_log_entries:
                    captains_selected = min(len(captains_log_entries) - 1, captains_selected + 1)
            elif key == curses.KEY_PPAGE:
                captains_selected = max(0, captains_selected - 10)
            elif key == curses.KEY_NPAGE:
                if captains_log_entries:
                    captains_selected = min(len(captains_log_entries) - 1, captains_selected + 10)
            elif key == curses.KEY_HOME:
                captains_selected = 0
            elif key == curses.KEY_END:
                if captains_log_entries:
                    captains_selected = len(captains_log_entries) - 1
            elif key == ord('\n') or key == curses.KEY_ENTER:
                if captains_log_entries:
                    captains_detail_open = True
            elif key == 27:  # Esc - unfocus
                panel_focus = FOCUS_NONE

        # Get events from event bus
        events_processed = 0
        while event_bus.event_available():
            event = event_bus.get_event(timeout=0)
            if event:
                events_processed += 1
                if event.event_type == event_bus.EventTypes.SHIP_SPOTTED:
                    handle_ship_spotted(event)
                elif event.event_type == event_bus.EventTypes.AIRCRAFT_SPOTTED:
                    handle_aircraft_spotted(event)
                elif event.event_type == event_bus.EventTypes.MESSAGE_RECEIVED:
                    handle_message_received(event)
                elif event.event_type == event_bus.EventTypes.WEATHER:
                    handle_weather(event)
                elif event.event_type == event_bus.EventTypes.CAPTAINS_LOG:
                    handle_captains_log(event)
                elif event.event_type == event_bus.EventTypes.CREW_UPDATE:
                    handle_crew_update(event)
                elif event.event_type == event_bus.EventTypes.LOCATION_UPDATE:
                    handle_location_update(event)
                elif event.event_type == event_bus.EventTypes.CLOCK_UPDATE:
                    handle_clock_update(event)
                elif event.event_type == event_bus.EventTypes.SPEED_UPDATE:
                    handle_speed_update(event)
                elif event.event_type == event_bus.EventTypes.HEADING_UPDATE:
                    handle_heading_update(event)
                elif event.event_type == event_bus.EventTypes.DEPTH_UPDATE:
                    handle_depth_update(event)
                elif event.event_type == event_bus.EventTypes.DEPTH_UNDER_KEEL_UPDATE:
                    handle_depth_under_keel_update(event)
                elif event.event_type == event_bus.EventTypes.HEARTBEAT:
                    handle_heartbeat(event)
                elif event.event_type == event_bus.EventTypes.SEARCH_STATUS:
                    handle_search_status(event)
                elif event.event_type == event_bus.EventTypes.ERROR:
                    handle_error(event)
                elif event.event_type == event_bus.EventTypes.STATUS:
                    handle_status(event)

        # Check if log needs redrawing
        log_needs_draw = (len(event_log) != last_log_count or events_processed > 0)
        if log_needs_draw:
            last_log_count = len(event_log)

        # Radio panel redraws when message count changes or it's focused
        radio_needs_draw = (len(radio_messages) != last_radio_count or panel_focus == FOCUS_RADIO)
        if len(radio_messages) != last_radio_count:
            last_radio_count = len(radio_messages)

        # Captain's log panel redraws when entry count changes or it's focused
        captains_needs_draw = (len(captains_log_entries) != last_captains_count or panel_focus == FOCUS_CAPTAINS)
        if len(captains_log_entries) != last_captains_count:
            last_captains_count = len(captains_log_entries)

        # Draw the panels (skipped while the terminal is too small; the
        # too-small message from relayout() stays on screen instead)
        if status_win is not None:
            # When a detail overlay closes, force all panels to fully re-copy
            # their content so no popup remnants remain on the virtual screen.
            # NOTE: stdscr must NOT be touched here - getch() implicitly calls
            # wrefresh(stdscr) when it is marked modified, which would blank the
            # entire virtual screen (stdscr's buffer is empty) and make all
            # static text (grid, titles, ...) invisible until it changes.
            detail_open = (radio_detail_open or captains_detail_open or
                           crew_screen_open or crew_detail_open)
            if prev_detail_open and not detail_open:
                for w in (status_win, log_win, radio_win, captains_win, footer_win):
                    w.touchwin()
            prev_detail_open = detail_open

            # Draw status panel (always redraw - speed/depth change)
            try:
                draw_status_panel(status_win, left_width)
            except curses.error:
                pass

            # Draw log panel (only if needed)
            if log_needs_draw or len(event_log) > 0:
                try:
                    draw_log_panel(log_win, log_height, left_width)
                except curses.error:
                    pass

            # Draw radio messages panel
            try:
                draw_radio_panel(radio_win, side_height, radio_width, panel_focus == FOCUS_RADIO)
            except curses.error:
                pass

            # Draw captain's log panel
            try:
                draw_captains_panel(captains_win, side_height, captains_width, panel_focus == FOCUS_CAPTAINS)
            except curses.error:
                pass

            # Draw footer (context-sensitive keybindings)
            try:
                draw_footer(footer_win, stdscr.getmaxyx()[1])
            except curses.error:
                pass

            # Draw the crew screen overlay on top of the panels (the
            # footer row stays visible with the crew keybindings)
            if crew_screen_open:
                try:
                    draw_crew_screen(stdscr)
                except curses.error:
                    pass

            # Draw detail overlays on top of everything if open
            if radio_detail_open and radio_messages:
                messages = list(radio_messages)
                idx = min(radio_selected, len(messages) - 1)
                try:
                    draw_radio_detail(stdscr, messages[idx])
                except curses.error:
                    pass
            elif captains_detail_open and captains_log_entries:
                entries = list(captains_log_entries)
                idx = min(captains_selected, len(entries) - 1)
                try:
                    draw_captains_detail(stdscr, entries[idx])
                except curses.error:
                    pass
            elif crew_detail_open and crew_state:
                members = list(crew_state)
                idx = min(crew_selected, len(members) - 1)
                try:
                    draw_crew_detail(stdscr, members[idx])
                except curses.error:
                    pass

        # Update all windows at once (no flickering)
        curses.doupdate()

        # Check if any collector has stopped
        for collector in collectors:
            if not collector.is_alive() and collector.running:
                add_log_event(f"WARNING: {collector.name} collector has stopped!", "error")
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                                       f"{collector.name} collector stopped unexpectedly")

        time.sleep(REFRESH_RATE)

def signal_handler(signum, frame):
    """Handle shutdown signals."""
    global running
    running = False
    add_log_event("Shutdown signal received", "info")

def main():
    """Main coordinator with ncurses UI."""
    global collectors
    
    print("=" * 60)
    print("   SH3 MASTER COORDINATOR - NCURSES VERSION")
    print("=" * 60)
    print("[Master] Starting collectors...")
    print("-" * 60)
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Import collector modules
    try:
        import collectors.ship_spotted as ship_spotted
        import collectors.aircraft_spotted as aircraft_spotted
        import collectors.speed as speed
        import collectors.depth as depth
        import collectors.depth_under_keel as depth_under_keel
        import collectors.message_received as message_received
        import collectors.location as location
        import collectors.heading as heading
        import collectors.weather as weather
        import collectors.clock as clock
        import collectors.patrol_log as patrol_log
        import collectors.captains_log as captains_log
        import collectors.crew as crew
    except ImportError as e:
        print(f"[Master] Failed to import collectors: {e}")
        return
    
    # Launch collector threads
    collectors = [
        CollectorThread("ShipSpotted", ship_spotted),
        CollectorThread("AircraftSpotted", aircraft_spotted),
        CollectorThread("Speed", speed),
        CollectorThread("Depth", depth),
        CollectorThread("DepthUnderKeel", depth_under_keel),
        CollectorThread("MessageReceived", message_received),
        CollectorThread("Location", location),
        CollectorThread("Heading", heading),
        CollectorThread("Weather", weather),
        CollectorThread("Clock", clock),
        CollectorThread("CaptainsLog", captains_log),
        CollectorThread("Crew", crew),
    ]
    # The patrol log writer (LLM recaps + wiki updates) is opt-in via
    # SH3_PATROL_LOG_ENABLED; it exits immediately with a status message
    # when disabled, so only spawn it when it is on.
    if env_config.env_flag('SH3_PATROL_LOG_ENABLED', default=False):
        collectors.append(CollectorThread("PatrolLog", patrol_log))
    
    for collector in collectors:
        collector.start()
        print(f"[Master] Started {collector.name} collector")
    
    print("-" * 60)
    print("[Master] All collectors running. Starting ncurses UI...")
    print("[Master] Press 'q' to quit")
    print(f"[Master] UI-phase output is logged to {UI_LOG_FILE}")
    print("=" * 60)
    
    # Initialize ncurses. While the curses UI is active, stdout/stderr are
    # redirected to a log file: prints from notify() or the collector
    # threads would otherwise land on the curses screen and corrupt it
    # (curses never repaints cells it does not know were overwritten).
    real_stdout, real_stderr = sys.stdout, sys.stderr
    try:
        with open(UI_LOG_FILE, 'a', buffering=1) as ui_log:
            sys.stdout = ui_log
            sys.stderr = ui_log
            # Run main loop with curses wrapper
            curses.wrapper(main_loop)
    except Exception as e:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        print(f"\n[Master] NCurses error: {e}")
        notify.trigger_notification(f"SH3 Master Error: {e}")
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        print("\n[Master] Shutting down...")
        event_bus.publish_event(event_bus.EventTypes.STATUS, "Master shutting down")

if __name__ == "__main__":
    main()
