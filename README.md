# SH3 Monitor - Silent Hunter 3 Monitoring System

A modular monitoring system for Silent Hunter 3 that tracks game state and sends notifications via XMPP.

## Architecture

The system uses a modular, event-driven architecture with four main components:

### 1. Collectors (Data Gathering)
Individual scripts that monitor specific game data:
- **collectors/ship_spotted.py** - Monitors for "Ship spotted, sir!" log messages
- **collectors/aircraft_spotted.py** - Monitors for "Aircraft spotted, sir!" log messages
- **collectors/message_received.py** - Monitors for "Radio Message Received!" log messages
- **collectors/weather.py** - Actively queries the Navigation Officer for weather reports (Ctrl-W via xdotool) and publishes them with the in-game date/time. Queries every 6 hours by default; override with `SH3_WEATHER_INTERVAL` (e.g. `900`, `90m`, `6h`, `1d`)
- **collectors/clock.py** - Polls the in-game clock UI string and publishes the current in-game date/time as `CLOCK_UPDATE` events (only on minute changes). Poll interval override: `SH3_CLOCK_INTERVAL` (default 30s)
- **collectors/captains_log.py** - Monitors the in-game captain's log array (the CAPTAIN'S LOG screen) and publishes entries as `CAPTAINS_LOG` events. Entries include patrol starts (`Patrol N\n<boat>\nLeft at: ...`), ships sunk, aircraft destroyed, and objectives completed. The patrol number from these entries drives the patrol-log writer's page selection
- **collectors/patrol_log.py** - LLM-generated Captain's Log: accumulates a digest per in-game day (sightings, radio traffic, weather, grids, dive/speed stats, notable captain's log entries) and, once per **real-time day at midnight**, writes a first-person recap for each day with unwritten events to the Gollum wiki. The target page is chosen by the **patrol number from the captain's log scanner**: each day goes to `Games/SH3 Captain's Log/Patrol N.md`, and a missing page (e.g. a new patrol) is created automatically. Out-of-sync games (time compression, restarts) are handled: multiple pending days each get their own entry, and a day spanning several real days is regenerated in place. Opt-in via `SH3_PATROL_LOG_ENABLED` (off by default). Config: `SH3_LLM_URL`, `SH3_LLM_KEY`, `SH3_LLM_MODEL`, `SH3_WIKI_URL`, `SH3_WIKI_DIR`, `SH3_WIKI_PAGE` (fixed-page override), `SH3_PATROL_JOURNAL`. Recap requests are streamed and tagged with the llama-swap priority header (`SH3_LLM_PRIORITY`, default 20), so a queued recap is kept alive by the scheduler's SSE keep-alive frames instead of timing out; `SH3_LLM_TIMEOUT` (default 1h) is a safety net only. See specs/captains-log.md
- **collectors/speed.py** - Monitors uboat speed
- **collectors/heading.py** - Monitors the uboat's course (heading float in the same player-boat structure as speed) and publishes `HEADING_UPDATE` events; the patrol log records big course changes (≥ 30° short-way around the compass) so recaps can note maneuvers
- **collectors/depth.py** - Monitors depth
- **collectors/depth_under_keel.py** - Monitors depth under keel
- **collectors/log_collector.py** - Generic log collector framework used by log-based collectors

Each collector:
- Runs in its own thread
- Publishes events to a shared event bus
- Can be tested independently
- Uses caching to remember discovered memory addresses

### 2. Event Bus (Communication)
- **event_bus.py** - Thread-safe event queue
- Handles communication between collectors and the master coordinator
- Supports multiple event types: ship_spotted, aircraft_spotted, message_received, weather, speed_update, depth_update, depth_under_keel_update, search_status, heartbeat, error, status

### 3. Master Coordinator (Orchestration)
- **sh3_master_ncurses.py** - Ncurses-based interface with split-screen layout
- Launches and manages all collector threads
- Handles events and triggers notifications
- Implements alert logic based on game state

### 4. Shared Utilities
- **utils.py** - Common memory reading functions used by all collectors
- **notify.py** - XMPP notifications; opt-in via `SH3_NOTIFY_ENABLED`, credentials from `SH3_XMPP_JID` / `SH3_XMPP_PASSWORD` / `SH3_XMPP_RECIPIENT` (see `.env.example`)

## Usage

### Running the Complete System

Run the ncurses master coordinator:

```bash
sudo python3 sh3_master_ncurses.py
```

This will:
1. Launch all collectors (ship_spotted, aircraft_spotted, radio, weather, clock, captain's log, speed, depth, depth_under_keel, location, heading, crew)
2. Display a split-screen interface with uboat status and event log
3. Show color-coded depth zones (green: 0-100m, yellow: 100-170m, red: 170m+)
4. Display search status and heartbeat indicators
5. Send XMPP notifications for configured alerts

Press 'q' to quit the ncurses interface.

### Running Individual Collectors

Each collector can be run independently for testing:

```bash
# Ship spotted monitor
sudo python3 collectors/ship_spotted.py

# Aircraft spotted monitor
sudo python3 collectors/aircraft_spotted.py

# Message received monitor
sudo python3 collectors/message_received.py

# Speed monitor
sudo python3 collectors/speed.py

# Depth monitor
sudo python3 collectors/depth.py

# Depth under keel monitor
sudo python3 collectors/depth_under_keel.py
```

## Configuration

### Environment / .env file

Runtime configuration — XMPP credentials, the LLM endpoint, and feature
switches — lives in environment variables, conveniently provided via a
`.env` file in the repo root (loaded automatically, no extra dependencies):

```bash
cp .env.example .env
# then edit .env and fill in your values
```

`.env` is gitignored; never commit real credentials. Real environment
variables override `.env` entries.

Key switches (both **off by default**):

- `SH3_NOTIFY_ENABLED` — XMPP notifications (`SH3_XMPP_JID`,
  `SH3_XMPP_PASSWORD`, `SH3_XMPP_RECIPIENT` required when on)
- `SH3_PATROL_LOG_ENABLED` — LLM-generated daily recaps written to a
  Gollum wiki (`SH3_LLM_URL`, `SH3_LLM_MODEL`, `SH3_WIKI_URL` required
  when on; see `specs/captains-log.md` for the full variable list)

See `.env.example` for every supported variable.

### Alert thresholds

Edit the master coordinator file to configure alerts:

```python
```python
# Thresholds for alerts
SHALLOW_DEPTH_THRESHOLD = 100.0  # meters (0-100m zone)
MEDIUM_DEPTH_THRESHOLD = 170.0  # meters (100-170m zone)
HIGH_SPEED_THRESHOLD = 20.0  # knots

# Notification settings
NOTIFY_SHIP_SPOTTED = True
NOTIFY_SHALLOW_DEPTH = False
NOTIFY_HIGH_SPEED = False
```

## Alert Logic

The master coordinator implements intelligent alert logic:

1. **Ship Spotted**: Always notifies when a ship is spotted
2. **Aircraft Spotted**: Always notifies when an aircraft is spotted
3. **Shallow Depth**: Warns when depth under keel drops below threshold (default: 100m)
4. **High Speed**: Warns when speed exceeds threshold (default: 20kts)
5. **Depth Zones**: Color-coded display shows depth zone (green/yellow/red)
1. **Ship Spotted**: Always notifies when a ship is spotted
2. **Aircraft Spotted**: Always notifies when an aircraft is spotted
3. **Shallow Depth**: Warns when depth under keel drops below threshold (default: 100m)
4. **High Speed**: Warns when speed exceeds threshold (default: 20kts)
5. **Depth Zones**: Color-coded display shows depth zone (green/yellow/red)

The ncurses UI has three columns: status + event log (left), radio messages
(middle), and captain's log entries (right). Press **Tab** to cycle panel
focus (none → radio → captain's log); a focused panel accepts arrow keys,
PageUp/PageDown, Home/End for scrolling, **Enter** to open the full entry in
a detail overlay, and **Esc** to unfocus.

### Crew Screen

Press **c** to open the crew screen: a full-screen overlay showing a side
cutaway of the U-boat (bow/stern torpedo rooms, crew quarters with bunks,
radio room, control room/Zentrale, officers' quarters, engine room, plus
conning tower with bridge and flak gun, and the deck gun). Each crew member
is a single glyph at their station, color-coded by fatigue (green fit /
yellow tired / red exhausted):

- `W L N R S T G M` — officers/petty officers by specialty (watch, LI,
  navigator, radio, sonar, torpedo, gunner, medic)
- `o` — enlisted crew on duty, `z` — off-duty crew resting in bunks

A roster list on the right shows each member's name, station, and fatigue
level; **Up/Down**, **PageUp/PageDown**, **Home/End** select a crewman and
**Enter** opens their details. The roster is **live**: the crew collector
reads the real crew (names, ranks, stations, fatigue) from game memory and
publishes it as `CREW_UPDATE` events. **d** loads/unloads a demo roster for
trying the screen without the game running; live data replaces it
automatically. **c**, **Esc**, or **q** closes the screen.

Crew rotation (manual and automatic when fatigue exceeds a threshold) is
planned.

## Adding New Collectors

The system uses a generic log collector framework that makes adding new collectors easy. There are two approaches:

### Approach 1: Using the LogCollector Framework (Recommended for Log-Based Collectors)

For collectors that monitor game log messages, use the unified LogCollector framework:

1. Create a new file in `collectors/` directory
2. Import the necessary modules and configure your collector:

```python
#!/usr/bin/env python3
"""
Your Custom Collector
Monitors SH3 game memory for your custom log messages.
"""
import sys
import os

# Import shared utilities, event bus, and log collector
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import event_bus
from collectors.log_collector import LogCollectorConfig, run_collector

# --- CONFIGURATION ---
SIG_TEXT = b'Your log message signature'
SIG_HEADER = b'\x75\x80'  # Optional: signature header (for backward compatibility)
STRIDE = 0x40  # Optional: stride (for backward compatibility)
FILTER_TEXT = "Your filter text"
CACHE_FILE = '.sh3_your_collector_cache.json'
MONITOR_INTERVAL = 5  # seconds between checks

def run():
    """Main collector entry point."""
    # Create configuration for your collector
    config = LogCollectorConfig(
        log_type="your_collector",
        sig_text=SIG_TEXT,
        sig_header=SIG_HEADER,
        stride=STRIDE,
        filter_text=FILTER_TEXT,
        event_type=event_bus.EventTypes.YOUR_EVENT_TYPE,  # Add to EventTypes in event_bus.py
        cache_file=CACHE_FILE,
        monitor_interval=MONITOR_INTERVAL
    )
    
    # Run the collector
    run_collector(config)

if __name__ == "__main__":
    run()
```

3. Add your event type to `event_bus.py` in the EventTypes class:

```python
class EventTypes:
    # ... existing event types ...
    YOUR_EVENT_TYPE = "your_event_type"
```

4. Add the collector to your master coordinator:

```python
# Import the collector
import collectors.your_collector as your_collector

# Add to collectors list
collectors = [
    # ... existing collectors ...
    CollectorThread("YourCollector", your_collector)
]
```

5. Add event handler in your master coordinator:

```python
def handle_your_event(event):
    """Handle your custom events."""
    message = event.data
    # Your logic here
    print(f"[Master] Your Event: {message}")
    if NOTIFY_YOUR_EVENT:
        notify.trigger_notification(f"SH3 Alert: {message}")
```

6. Add the event routing in the event loop:

```python
if event.event_type == event_bus.EventTypes.YOUR_EVENT_TYPE:
    handle_your_event(event)
```

### Approach 2: Custom Collector

For collectors that don't monitor logs (like speed or depth), create a custom collector:

1. Create a new file in `collectors/` directory
2. Import `utils` and `event_bus`
3. Use `event_bus.publish_event()` to send data
4. Follow the pattern of existing collectors like `speed.py` or `depth.py`

## File Structure

```
shHelper/
├── utils.py                    # Shared memory functions
├── event_bus.py               # Event queue and classes
├── notify.py                  # XMPP notifications
├── sh3_master_ncurses.py      # NCurses master coordinator
├── collectors/
│   ├── log_collector.py       # Generic log collector framework
│   ├── ship_spotted.py        # Ship spotted events
│   ├── aircraft_spotted.py    # Aircraft spotted events
│   ├── message_received.py    # Message received events
│   ├── weather.py             # Weather reports (active Ctrl-W queries)
│   ├── speed.py               # Speed monitoring
│   ├── depth.py               # Depth monitoring
│   └── depth_under_keel.py    # Depth under keel monitoring
├── memory-tools/
│   ├── diagnostic_scan.py     # Diagnostic memory scanning
│   ├── ship_spotted_scan.py   # Ship spotted memory scanning
│   ├── smart_dump_v2.py       # Memory dumping tool
│   └── *.dump                 # Memory dump files
├── .sh3_ship_spotted_cache.json      # Cache for ship spotted addresses
└── .sh3_aircraft_spotted_cache.json  # Cache for aircraft spotted addresses
```

## Memory Tools

The `memory-tools/` directory contains diagnostic and scanning utilities:

- **diagnostic_scan.py** - General memory diagnostic scanning
- **ship_spotted_scan.py** - Specific scanner for ship spotted log addresses
- **smart_dump_v2.py** - Advanced memory dumping tool
- ***.dump** - Various memory dump files for analysis

These tools are useful for discovering new memory addresses and analyzing game memory structures.

## Requirements

- Python 3.x
- slixmpp (for XMPP notifications)
- xdotool (for the weather collector's Ctrl-W queries)
- Root/sudo access (required for reading process memory)

## Troubleshooting

### "Could not find SH3 process"
- Make sure Silent Hunter 3 is running
- Check that the process name is "sh3.exe"

### "Memory path does not exist"
- Run with sudo/root privileges
- Ensure you have permission to read /proc/[pid]/mem

### Collector stopped unexpectedly
- Check the error message in the master output
- Try running the collector individually to debug
- Check cache files - they may have stale addresses from a previous game session

### Discovery takes too long
- The log collectors use a two-phase discovery process that scans game memory
- First phase finds seed addresses, second phase scans windows around those seeds
- Progress updates are displayed in the ncurses version
- Cached addresses are used when available to speed up startup

### NCurses display issues
- Ensure your terminal is at least 18 rows x 60 columns
- Try resizing your terminal if the display is distorted
- Press 'q' to quit cleanly

## Event Types

The system supports the following event types:

- **ship_spotted** - Ship spotted message detected
- **aircraft_spotted** - Aircraft spotted message detected
- **message_received** - Radio message received
- **weather** - Weather report from the Navigation Officer (in-game date/time + report text)
- **speed_update** - Uboat speed update
- **depth_update** - Depth update
- **depth_under_keel_update** - Depth under keel update
- **search_status** - Log search status (searching/complete/failed)
- **heartbeat** - Collector heartbeat for health monitoring
- **error** - Error event from collectors
- **status** - General status message

## License

This project is for educational purposes and monitoring a game you own.
