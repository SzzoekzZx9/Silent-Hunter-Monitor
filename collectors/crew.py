#!/usr/bin/env python3
"""
Crew Collector
Monitors SH3 game memory for the live crew roster: names, compartments,
morale, and fatigue. Publishes CREW_UPDATE events to the event bus.

Memory layout (discovered via memory-tools, see issues #12 and #22):
  - Roster table lives in static EXE data at 0x5F6154 (stable across game
    restarts, same sh3.exe build).
  - Entry stride: 0xE4 (228) bytes, ~140 entries.
  - Entry fields:
      +0x00  name      null-terminated ASCII, <= 0x34 bytes
      +0x34  index     u32, crewman index (0 for most enlisted)
      +0x38  nrc       i32, compartment ID (-1 = stale/inactive copy)
      +0x44  rank_tier u32 (2 = officer, 1 = senior enlisted, 0 = enlisted)
      +0x50  morale    f32 (0..1)
      +0x54  fatigue   f32 (0..1, live — ticks in real time)
  - The game keeps duplicate entries per crewman; the live entry is the one
    with nrc != -1. Crew are always looked up by name, never by index.
  - Endurance (as displayed in-game) = 1.0 - fatigue.

POSITION MAP (v3, issue #22 — the primary compartment source):
  A roster entry's position in the table is a GLOBAL SLOT NUMBER: every
  station and bunk in every compartment has a fixed position, and the game
  moves entries between positions on every crew move — drags, swap-drags
  (both crewmen), and game-initiated bulk moves (dive/surface watch
  changes). This was validated with a live 1s watch of a full crew
  reshuffle: every displaced crewman landed in the correct compartment's
  position block even while his nrc stayed stale.
  Position map (calibrated 2026-09-29, Type VII, user-validated slot
  counts — 94 slots total). Positions 3-12 hold one primary slot per
  compartment in crew-screen order; the rest are contiguous blocks:
      1, 14-17    bridge (conning tower)          5 slots
      18-20       deck gun                        3 slots
      21          flak gun                       1 slot
      29-30       sonar/radio stations (radio room) 2 slots
      3-5, 31-33  command room                    6 slots
      6, 34-41    diesel engine room              9 slots
      7, 44-51    electric motor room             9 slots
      8, 54-67    bow torpedo room               15 slots
      9, 68-83    crew quarters (bow bunks)      17 slots
      10, 84-91   stern quarters                  9 slots
      11, 100-105 stern torpedo room              7 slots
      12, 108-117 damage control team            11 slots
  Unmapped positions (0, 2, 13, 22-28, 42-43, 52-53, 92-99, 106-107,
  118+) are not crew slots; if one ever holds a live entry the collector
  falls back to nrc for it.

NRC STALENESS (the bug this position map fixes): nrc is written when a
crewman is directly dragged or moved by the game, but a crewman displaced
by a swap-drag keeps (or gets a wrong) nrc — e.g. dragging X onto Y's
station writes the target compartment into X's entry and moves Y's entry
to X's old position, leaving Y's nrc pointing at his old compartment
forever. Observed live: Gabelmann displaced off a bridge slot kept nrc=0
(bridge) while sitting in a quarters bunk at position 68; Haas kept nrc=6
(quarters) at the stern torpedo position 101. The position map corrects
all of these; the slot map below still refines exact stations.

Slot map (v2, issue #12): a static array of 34 slot-descriptor nodes at
0x546284 (stride 0x64) holds the occupant's name at +0x00 (empty = vacant)
and a pointer to the slot's rendering record at +0x38. Each node is a
FIXED station slot (bridge/command/sonar/radio/deck gun/flak — the
3D-rendered stations). It supersedes both position and nrc for labeled
nodes and distinguishes the radio station from the sonar station (both
compartment 1). No static slot structures exist for
diesel/e-motor/torpedo/quarters/damage-control (verified by full static
scans) — those crew are placed by the position map.

Compartment IDs (empirical, from user-validated controlled moves):
  0 conning tower/bridge, 1 radio room (sonar + radio stations),
  2 command room, 3 diesel engine room, 4 electric motor room,
  5 bow torpedo room, 6 crew quarters (bow), 7 stern quarters,
  8 stern torpedo room, 9 deck gun, 10 flak gun, 11 damage control team.
"""
import os
import struct
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus

# --- CONFIGURATION ---
ROSTER_BASE = 0x5F6154
ROSTER_STRIDE = 0xE4
ROSTER_COUNT = 140
UPDATE_INTERVAL = 2.0  # seconds between updates

OFF_NAME = 0x00
OFF_INDEX = 0x34
OFF_NRC = 0x38
OFF_RANK = 0x44
OFF_MORALE = 0x50
OFF_FATIGUE = 0x54

COMPARTMENTS = {
    0: "Conning Tower",
    1: "Radio/Sonar Room",
    2: "Command Room",
    3: "Diesel Engine Room",
    4: "Electric Motor Room",
    5: "Bow Torpedo Room",
    6: "Crew Quarters",
    7: "Stern Quarters",
    8: "Stern Torpedo Room",
    9: "Deck Gun",
    10: "Flak Gun",
    11: "Damage Control Team",
}

# UI (sh3_master_ncurses.py crew screen) station keys per compartment ID.
# The cutaway has no separate sonar room: sonar crew display under the
# control room. Radio-station crew are switched to 'radio_room' by the
# slot map in apply_slot_overrides().
STATION_OF = {
    0: 'bridge',
    1: 'control_room',
    2: 'control_room',
    3: 'engine_room',
    4: 'engine_room',
    5: 'bow_torpedo',
    6: 'crew_quarters',
    7: 'crew_quarters',
    8: 'stern_torpedo',
    9: 'deck_gun',
    10: 'flak',
    11: 'damage_control',
}
RANK_NAMES = {2: 'Officer', 1: 'Petty Officer', 0: 'Enlisted'}

# --- POSITION MAP (issue #22) ---
# Roster table position -> compartment ID. Inclusive (first, last, cid)
# ranges, calibrated 2026-09-29 from a user-validated live crew reshuffle.
# Positions not listed fall back to nrc.
POSITION_RANGES = [
    (1, 1, 0),      # bridge watch
    (3, 5, 2),      # command room stations
    (6, 6, 3),      # diesel engine room
    (7, 7, 4),      # electric motor room
    (8, 8, 5),      # bow torpedo room
    (9, 9, 6),      # crew quarters bunk
    (10, 10, 7),    # stern quarters bunk
    (11, 11, 8),    # stern torpedo room
    (12, 12, 11),   # damage control team
    (14, 17, 0),    # bridge
    (18, 20, 9),    # deck gun
    (21, 21, 10),   # flak gun
    (29, 30, 1),    # sonar / radio stations (radio room)
    (31, 33, 2),    # command room stations
    (34, 41, 3),    # diesel engine room
    (44, 51, 4),    # electric motor room
    (54, 67, 5),    # bow torpedo room
    (68, 83, 6),    # crew quarters (bunks)
    (84, 91, 7),    # stern quarters
    (100, 105, 8),  # stern torpedo room
    (108, 117, 11), # damage control team
]
# User-verified slot counts per compartment (in-game crew screen).
POSITION_SLOT_COUNTS = {0: 5, 1: 2, 2: 6, 3: 9, 4: 9, 5: 15, 6: 17,
                        7: 9, 8: 7, 9: 3, 10: 1, 11: 11}
POSITION_COMPARTMENTS = {}
for _lo, _hi, _cid in POSITION_RANGES:
    for _p in range(_lo, _hi + 1):
        POSITION_COMPARTMENTS[_p] = _cid

# --- SLOT MAP (static node array) ---
SLOT_ARRAY_BASE = 0x546284
SLOT_COUNT = 34
SLOT_STRIDE = 0x64

# Node address -> compartment, calibrated from user-confirmed ground truth
# (2026-09-28/29 sessions). Vacant/unconfirmed nodes are absent.
SLOT_LABELS = {
    0x5462e8: 2,   # command (Adolf Kessler)
    0x54634c: 2,   # command (Franz Linke-Stier)
    0x546414: 2,   # command
    0x546478: 2,   # command (Joahim Loche)
    0x5464dc: 2,   # command (Ernst Heldmann -> Harald Schubert)
    0x546540: 1,   # radio station (radio room, compartment 1)
    0x5465a4: 1,   # sonar station (radio room, compartment 1)
    0x546608: 0,   # bridge (Wolfgang Eisenholtz)
    0x54666c: 0,   # bridge (Joachim Kudelka)
    0x5466d0: 0,   # bridge (Hans-Bernhard Wintermeyer)
    0x546734: 0,   # bridge (Claus Bieldstein)
    0x546798: 0,   # bridge (Erich Ditz)
    0x5467fc: 9,   # deck gun (Jurgen Mannesmann)
    0x546860: 9,   # deck gun (Gotthard Bekker)
    0x5468c4: 9,   # deck gun (Gustaw Endelmann)
    0x546928: 10,  # flak gun (Hans-Bernhard Wintermeyer)
}
RADIO_SLOT_NODE = 0x546540


def _valid_name(raw: bytes) -> str:
    """Return the decoded name if valid, else ''. Valid: printable ASCII,
    3..31 chars, mostly letters (allows spaces, hyphens, apostrophes)."""
    z = raw.find(b'\x00')
    if z < 0:
        z = len(raw)
    try:
        name = raw[:z].decode('ascii')
    except UnicodeDecodeError:
        return ''
    if not (3 <= len(name) <= 31):
        return ''
    ok = sum(c.isalpha() or c in " -'" for c in name)
    if ok < len(name) * 0.8:
        return ''
    return name.strip()


def parse_roster(data: bytes):
    """Parse the raw roster table bytes into a list of live crew dicts.

    Pure function (no process access) so it can be unit-tested.
    Returns only live entries (nrc != -1, valid name, sane floats),
    deduplicated by name (first occurrence wins). Each crew dict carries
    its table 'position' (the global slot number, see module docstring);
    callers apply position/slot overrides for the true compartment.
    """
    crew = []
    seen = set()
    count = len(data) // ROSTER_STRIDE
    for n in range(count):
        e = data[n * ROSTER_STRIDE:(n + 1) * ROSTER_STRIDE]
        name = _valid_name(e[OFF_NAME:OFF_NAME + 0x34])
        if not name:
            continue
        idx = struct.unpack_from('<I', e, OFF_INDEX)[0]
        nrc = struct.unpack_from('<i', e, OFF_NRC)[0]
        if nrc == -1:
            continue  # stale/inactive copy
        rank = struct.unpack_from('<I', e, OFF_RANK)[0]
        morale = struct.unpack_from('<f', e, OFF_MORALE)[0]
        fatigue = struct.unpack_from('<f', e, OFF_FATIGUE)[0]
        if not (0.0 <= morale <= 1.0 and 0.0 <= fatigue <= 1.0):
            continue  # garbage entry
        if name in seen:
            continue
        seen.add(name)
        crew.append({
            'name': name,
            'index': idx,
            'position': n,
            'compartment_id': nrc,
            'compartment': COMPARTMENTS.get(nrc, f"Unknown ({nrc})"),
            'rank_tier': rank,
            'morale': round(morale, 3),
            'fatigue': round(fatigue, 3),
            'endurance': round(1.0 - fatigue, 3),
            # UI-compatible fields (sh3_master_ncurses.py crew screen);
            # recomputed by apply_position_overrides/apply_slot_overrides.
            'station': STATION_OF.get(nrc),
            'rank': RANK_NAMES.get(rank, 'Enlisted'),
            'on_duty': nrc not in (6, 7),   # quarters compartments = resting
            'officer': rank == 2,
            'specialty': None,
        })
    return crew


def read_roster(pid: int) -> bytes:
    """Read the raw roster table from the game process."""
    return utils.read_memory_chunk(pid, ROSTER_BASE, ROSTER_COUNT * ROSTER_STRIDE)


def _recompute_ui_fields(c):
    """Refresh station/on_duty from the (possibly overridden) compartment."""
    cid = c['compartment_id']
    c['station'] = STATION_OF.get(cid)
    c['on_duty'] = cid not in (6, 7)


def apply_position_overrides(crew):
    """Compartment from the roster table position (global slot number).

    The game moves entries to the target compartment's position block on
    every move — including crew displaced by swap-drags, whose nrc goes
    stale. Positions outside the calibrated map keep nrc.
    """
    for c in crew:
        cid = POSITION_COMPARTMENTS.get(c['position'])
        if cid is not None:
            c['compartment_id'] = cid
            c['compartment'] = COMPARTMENTS[cid]
            c['position_source'] = 'position'
        else:
            c['position_source'] = 'nrc'
        _recompute_ui_fields(c)
    return crew


def parse_slots(data: bytes):
    """Parse the static slot-node array into {occupant_name: node_addr}.

    Pure function. Only returns names that plausibly match roster crew
    (filters the 'ent' placeholder and garbage). Vacant slots (empty name)
    are skipped.
    """
    occupants = {}
    for n in range(SLOT_COUNT):
        node = data[n * SLOT_STRIDE:(n + 1) * SLOT_STRIDE]
        name = _valid_name(node[:0x34])
        if len(name) < 4:  # 'ent' placeholder and short garbage
            continue
        occupants[name] = SLOT_ARRAY_BASE + n * SLOT_STRIDE
    return occupants


def read_slots(pid: int) -> bytes:
    """Read the raw slot-node array from the game process."""
    return utils.read_memory_chunk(pid, SLOT_ARRAY_BASE, SLOT_COUNT * SLOT_STRIDE)


def apply_slot_overrides(crew, slot_occupants):
    """Merge slot-map compartments into a roster (in place).

    A crewman found in a labeled slot node gets that node's compartment
    (source 'slot'); one in an unlabeled covered slot keeps his position/
    nrc compartment but is flagged; everyone else keeps it (source 'nrc').
    The radio slot node additionally switches the UI station to
    'radio_room' (sonar and radio share compartment 1).
    """
    for c in crew:
        node = slot_occupants.get(c['name'])
        if node is not None:
            c['slot_node'] = node
            label = SLOT_LABELS.get(node)
            if label is not None:
                c['compartment_id'] = label
                c['compartment'] = COMPARTMENTS[label]
                c['slot_source'] = 'slot'
            else:
                c['slot_source'] = 'slot-unlabeled'
        else:
            c['slot_source'] = 'nrc'
        _recompute_ui_fields(c)
        if node == RADIO_SLOT_NODE:
            c['station'] = 'radio_room'
    return crew


def run():
    """Main collector loop."""
    pid = utils.get_pid_by_name('sh3.exe')
    if not pid:
        event_bus.publish_event(event_bus.EventTypes.ERROR,
                                "Crew collector: Could not find SH3 process")
        return

    try:
        while True:
            try:
                crew = apply_position_overrides(parse_roster(read_roster(pid)))
                try:
                    crew = apply_slot_overrides(
                        crew, parse_slots(read_slots(pid)))
                except OSError:
                    pass  # slot array unreadable: position map still applied
                event_bus.publish_event(
                    event_bus.EventTypes.CREW_UPDATE,
                    {
                        'crew': crew,
                        'count': len(crew),
                        'timestamp': datetime.now().isoformat(),
                    }
                )
            except Exception as e:
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                                        f"Crew collector error: {e}")
                time.sleep(5)
                continue

            time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        event_bus.publish_event(event_bus.STATUS,
                                "Crew collector stopped")


if __name__ == "__main__":
    run()