# Issue #17 Investigation: Live Grid/Position Source in SH3 Memory

**Story**: Gitea issue #17 — location collector reads stale grid from dead static address
**Game**: SH3 (GWX-Onealex-Edition-1.53) under Wine/Lutris, live pid 2264
**Boat**: U-45, patrol grid BF15 (Bay of Biscay), started Wilhelmshaven, outbound through North Sea
**Grid history (user-reported)**: AM43 → AM27 → AM71 → AM74 (current as of 2026-09-19)

---

## 1. The Bug

`collectors/location.py` reads a grid string from `GRID_PTR = 0x005546d0` — frozen at
`b'AN98\x00...'`. The static block around it is **partially alive**:

| Address | Status | Content |
|---|---|---|
| `0x5546d0` | **DEAD** | stale grid string "AN98" |
| `0x554698` | **LIVE** | pointer → boat struct base (changes between sessions: `0xa922cfd0`, `0xa8d0a720`) |
| boat base `+0x64` | LIVE | heading (degrees float) |
| boat base `+0x68` | LIVE | speed (knots float) |
| `0x5543dc +0xc4` | LIVE | depth under keel |
| `0x1d2f308` | LIVE | depth |

---

## 2. LIVE Boat Position Found (best result so far)

**`0x3b529d40` / `0x3b529d48`** — adjacent float pair (x, z), the boat's live position.

- Drift ratio matches heading exactly: Δx/Δz = 0.471 ≈ tan(205.2°) while heading 205.2°
- **Units: 1 float unit = 10 meters** (calibrated twice: at 8x TC over 45s, and 1x over 30s)
- **Floating origin**: values are small (±300) and **rebase** — observed jump of
  (+106, +220) units opposite to travel direction. Origin moves; absolute position unknown.
- Struct context around the pair (offsets from `0x3b529d40`):
  - `+0x000` / `+0x008`: position (x, z), 10m units, local
  - `+0x044..+0x08c`: small values ~0.0033
  - `+0x0a0`: 0.02, `+0x0a4`: 300.0
  - `+0x0b0`: -0.9050, `+0x0b8`: -0.4254 → **cos/sin(heading)** direction vector
  - `+0x0d0`: -0.9050 (cos again)
  - `+0x0d4/+0x0d8/+0x0dc`: second position triple (-86.02, **-300.0054**, -151.43) —
    x, y?, z copy; y = -300 constant (units/scale unknown, maybe camera or depth-related)
  - `+0x108..+0x13c`: 0.0625/0.125/0.1875, 1.0 — small tables
  - `-0x140..-0x100`: 0.47..1.20 ascending — wave/sea table?

**Other drifting copies found:**
- `0x62ed8418` / `0x62ed841c` — drifts at **91% of boat speed**, axes swapped
  (ratio = cot(heading)) → likely camera or trailing entity
- `0x61ae0c90` / `0x61ae0c98` — drifted during first scan, **frozen** in later sample →
  transient render-side copy, unreliable

**Open problem**: the floating origin. Absolute position = origin + local×10m.
The origin (or sector indices) must be stored somewhere — not yet found.

**Rebase behavior (observed 2026-09-19)**: after a rebase the local coords restart
near (0, 0) — e.g. (−85, −151) → (−6, −0.04) after the user (probably) opened the nav
map / changed course. Hypothesis: origin snaps to (near) the boat's current position,
possibly on screen transitions. If so, local position = displacement since last rebase,
and rebases are detectable as large jumps toward zero.

---

## 3. Ruled-Out Sources (do not revisit)

| Source | Verdict |
|---|---|
| `GRID_PTR 0x5546d0` static string | dead, never updated |
| `0x3b3e–0x3b46xxxx` "AM43" records | parsed **Flotilla.cfg** patrol-grid assignments by date (key = `Nov0121=AM43`), config not state |
| `b'Entered grid'` strings | **0 hits** in memory — game doesn't write them |
| Grid-name tables `0x2c1f–0x2c21xxxx` | static lists, 10-byte stride (`"AM74\0"×6`), grid groups from config. Parallel copies in 0x2c20/0x2c21 blocks offset 0xe6; paired entries 0x32 apart. No floats, no geometry |
| `0x2c199d0d` "BF15"+floats record | grid **properties** (weather/sea state): 70, 70, 60, -1, 1, -10, 100, 350, -4992, 100, 200, 350, 500, 300, 500, 600, 1000, 160, 200, 300, 600, 300, 500, 600, 1000, 200, 550, 700, ~1652 — NOT lat/long bounds |
| `0x2c286f30` mission state | `"U-45"` + `"BF15"` — assigned patrol grid, static. Nearby ints look date-ish (0x7ea, 9, 19, 18, 11, 0x2f0) |
| `"Grid XXXX"` template `0x3e8da970` | **target screen** label (near "Target" `0x3e8da910`, "Buttons", "Help") — not player grid |
| Live "current grid" string | **does not exist**: scans for AM74/AM71/AM27 — all hits inside the static clusters above. Current grid is NOT stored as a string |
| Adjacent float lat/long pair | 41,232 candidates scanned; 192 in N Sea box; order-insensitive diff across a grid crossing → only 49 changed, none a plausible live N Sea lat/long. **No adjacent-float lat/long for the boat** |
| Adjacent double lat/long pair | 11,643 "hits" but essentially all have lon = 0.000000 (zero padding matching the range filter) — no real double pair found |
| Near-float (±64B) lat/long | 307k hits, dominated by zeros — too noisy, inconclusive |

**Static data of note**: `0x00542a20` = 55.8167 (float, static region near the live static block) with zeros around — significance unknown.

---

## 4. Confirmed Static/Reference Data

- **Coastline vertex arrays** in `0x78–0x7f` region: floats in **lat/long degrees**
  (Scotland/Orkney ~58N, France, Norway, Sardinia, Iceland). The map/render pipeline
  uses lat/long floats — so a world→lat/long conversion exists in the engine.
- **UI templates** (`data/Menu/en_menu.txt`): `%s. Position %s` (lines 1882, 1901),
  "Request for New Grid" (1477-1484), "New patrol grid XXXX assigned. Good luck!" (1482),
  `|Mission Orders: Patrol grid %s` (1877)
- **German grid format**: Großquadrat (AM) + Mittelquadrat (43) + Kleinquadrat (55) → "AM 4355".
  Flotilla.cfg and user reports use Mittelquadrat level (AM74)
- **Audio-phase float** `0x4ed50008`: advances when unpaused — usable as pause probe,
  but froze once while clock still advanced; **game clock (`utils.find_ui_clock`) is the
  more reliable liveness probe**
- Mission clock observed: 1940-09-21, advancing

---

## 5. Tools Built (all memory-safe: page digests, stream pages, never hold snapshots)

| Tool | Purpose |
|---|---|
| `memory-tools/memmap.py` | map explorer |
| `memory-tools/snapdiff.py` | snapshot differ |
| `memory-tools/drift_scan.py` | generic drift scanner (digest passes + re-read changed pages) |
| `memory-tools/pos_drift_scan.py` | position drift scanner; waits for game running via UI clock; reports drifting floats/pairs/64KB blocks. Usage: `python3 -u pos_drift_scan.py <pid> <interval>` |
| `memory-tools/grid_watch.py` | **background watcher**: samples position pair + heading/speed every 0.5s → `/tmp/grid_watch.csv`; digests all rw pages every 45s, logs per-interval changed-page sets → `/tmp/grid_watch_intervals.log`. Baseline: ~1,400–1,900 pages change per 45s during normal cruise |

**Scanner log**: `/tmp/pos_drift.log` (last run: 1633 changed pages, 91,285 drifting floats)

**Memory-safety rule (critical)**: an earlier scanner crashed the server holding ~1.9GB
snapshots. Always stream 4KB pages, keep only sha1 digests + candidate addresses.

---

## 6. Key Insight Still To Exploit

The mission engine **knows** the current grid (patrol-grid entry/exit detection,
"Request for New Grid", 24h patrol timing). Since it's not a string, it is likely:
1. an **integer index/ID** compared against grid geometry, or
2. derived on demand from absolute position.

## 7. Next Steps

1. **Crossing-diff to find the grid ID** (IN PROGRESS — `grid_watch.py` running):
   per-interval changed-page sets are being logged. When the user crosses into the next
   grid, candidates = pages changed in the crossing interval − pages changed in cruising
   intervals → examine for a small int / grid-table pointer that changed exactly once.
   User must report the new grid + approximate time.
2. **Find the floating origin**: hunt for a stored sector origin / world offset near the
   boat struct or in globals (large ints/floats that jump when the local position rebases).
3. **Grid geometry**: if grid polygons exist in world coords anywhere, matching the
   boat's world position gives the grid directly.
4. Once a live source is proven, update `collectors/location.py` to publish
   `LOCATION_UPDATE` (consumed by `collectors/patrol_log.py`), replacing `GRID_PTR`.

---

*Last updated: 2026-09-19, during live session (boat in AM74, cruising ~10kn heading ~205°)*