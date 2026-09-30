#!/usr/bin/env python3
"""Ported SH3 grid transform (issue #17).

Reverse-engineered from sh3.exe (pid 2543 session):
  top-level dispatch .... 0x49f9b2  (lat bounds, row calc, region dispatch)
  regular handler ....... 0x497890  (rows < 99, western waters)
  NS/Baltic handler ..... 0x49ca80  (rows 72-98, eastern waters)  [NOT decoded]
  N-Atlantic handler .... 0x49cd30  (rows >= 99) + digit fns 0x4979d0-0x499c00
  trunc helper .......... 0x517824  (trunc toward zero, NOT floor)

Conventions: lat = degrees north; lon_w = degrees WEST positive
(the game's internal longitude, e.g. 15.0 = 15°W / -6.5 = 6.5°E).

Regular region (rows < 99, western):
  row  = trunc((lat + 38.099998474121094) * 1.1111111405455043)
  band = row // 9, rib_i = row % 9
  col  = trunc((BAND_BOUND[band] - lon_w) / BAND_WIDTH[band])
  grid = LETTERS[band][col // 9] + " " + str(RIBS[rib_i][col % 9])

North Atlantic (rows >= 99): per-row bound/letter table; letter_c covers
lon_w in [bound_{c+1}, bound_c) with value = bound_c - lon_w; mittelquadrat
digit = TABLE[case][trunc(value * scale)] where case depends on row and the
letter's digit function.  Simple functions ported: AH, AJ, AK, AM, AE, AF, AG.
Complex (stack-table) functions NOT ported: AD, AL, AN, AO, BH.
"""
import math

# ---------------------------------------------------------------- constants
LAT_MIN = -36.45000076293945   # 0x528ca8
LAT_MAX = 67.0                 # 0x528cb0
ROW_SUB = -38.099998474121094  # 0x528700 (fsub -> lat - this)
ROW_MUL = 1.1111111405455043   # 0x528c98

# regular region: 11 bands (rows 0..98), bound/width doubles at 0x53d018/0x528698
BAND_BOUND = [64.0, 52.3, 42.4, 42.4, 52.3, 100.9, 100.9, 100.9, 99.1, 78.7, 79.0]
BAND_WIDTH = [1.2, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 1.2, 1.2, 1.5]

# letter strings per band (ptr table 0x53cd40, 14 slots per band)
LETTERS = [
    ['GK', 'GL', 'GM', 'GN', 'GO', 'GP', 'GQ', 'GR'],           # band 0
    ['GA', 'GB', 'GC', 'GD', 'GE', 'GF', 'GG', 'GH', 'GJ'],     # band 1
    ['FQ', 'FR', 'FS', 'FT', 'FU', 'FV', 'FW'],                 # band 2
    ['FJ', 'FK', 'FL', 'FM', 'FN', 'FO', 'FP'],                 # band 3
    ['FA', 'FB', 'FC', 'FD', 'FE', 'FF', 'FG', 'FH'],           # band 4
    ['PB', 'PC', 'EL', 'EM', 'EN', 'EO', 'EP', 'EQ', 'ER', 'ES', 'ET', 'EU', 'EV', 'EW'],  # 5
    ['PA', 'EA', 'EB', 'EC', 'ED', 'EE', 'EF', 'EG', 'EH', 'EJ', 'EK'],                   # 6
    ['DK', 'DL', 'DM', 'DN', 'DO', 'DP', 'DQ', 'DR', 'DS', 'DT', 'DU'],                   # 7
    ['DA', 'DB', 'DC', 'DD', 'DE', 'DF', 'DG', 'DH', 'DJ'],     # band 8
    ['CA', 'CB', 'CC', 'CD', 'CE', 'CF', 'CG'],                 # band 9
    ['BA', 'BB', 'BC', 'BD', 'BE'],                             # band 10
]

# mittelquadrat digit tables, rib[rib_i][col % 9]  (0x528738..0x528878)
RIBS = [
    (77, 78, 79, 87, 88, 89, 97, 98, 99),   # rib 0
    (74, 75, 76, 84, 85, 86, 94, 95, 96),   # rib 1
    (71, 72, 73, 81, 82, 83, 91, 92, 93),   # rib 2
    (47, 48, 49, 57, 58, 59, 67, 68, 69),   # rib 3
    (44, 45, 46, 54, 55, 56, 64, 65, 66),   # rib 4
    (41, 42, 43, 51, 52, 53, 61, 62, 63),   # rib 5
    (17, 18, 19, 27, 28, 29, 37, 38, 39),   # rib 6
    (14, 15, 16, 24, 25, 26, 34, 35, 36),   # rib 7
    (11, 12, 13, 21, 22, 23, 31, 32, 33),   # rib 8
]

# eastern-lon thresholds: rows 72-80 -> 0.0, 81-89 -> 24.5, 90-98 -> 11.3
# (lon_w below threshold => NS/Baltic handler 0x49ca80, not decoded)
EASTERN_THRESHOLD = [(72, 81, 0.0), (81, 90, 24.5), (90, 99, 11.3)]

# N-Atlantic row table (0x527c70, 16-byte entries: bound double, letter dword)
# per row 99..116: list of (bound_c, letter_code); letter c covers
# lon_w in [bound_{c+1}, bound_c); last entry is the -30 terminator.
NS_LETTER_OF = {1: 'AD', 2: 'AE', 3: 'AF', 4: 'AG', 5: 'AH', 6: 'AJ',
                7: 'AK', 8: 'AL', 9: 'AM', 10: 'AN', 11: 'AO', 19: 'BH'}
NS_ROWS = {
    99:  [(67.0, 5), (53.5, 6), (40.0, 7), (26.5, 8), (16.0, 9), (2.5, 10), (-6.5, 19), (-30.0, 0)],
    100: [(67.0, 5), (53.5, 6), (40.0, 7), (26.5, 8), (16.0, 9), (2.5, 10), (-9.5, 19), (-30.0, 0)],
    101: [(67.0, 5), (53.5, 6), (40.0, 7), (26.5, 8), (16.0, 9), (2.5, 10), (-9.5, 11), (-17.0, 19), (-30.0, 0)],
    102: [(67.0, 5), (53.5, 6), (40.0, 7), (26.5, 8), (16.0, 9), (2.5, 10), (-9.5, 11), (-30.0, 0)],
    103: [(67.0, 5), (53.5, 6), (40.0, 7), (26.5, 8), (16.0, 9), (2.5, 10), (-9.5, 11), (-30.0, 0)],
    104: [(67.0, 5), (53.5, 6), (40.0, 7), (26.5, 8), (16.0, 9), (4.0, 10), (-9.5, 11), (-30.0, 0)],
    105: [(69.5, 5), (53.5, 6), (37.2, 7), (26.5, 8), (15.5, 9), (5.0, 10), (-9.5, 11), (-30.0, 0)],
    106: [(69.5, 5), (53.5, 6), (37.2, 7), (26.5, 8), (15.5, 9), (5.0, 10), (-9.5, 11), (-30.0, 0)],
    107: [(69.5, 5), (53.5, 6), (37.2, 7), (26.5, 8), (15.5, 9), (5.0, 10), (-9.5, 11), (-30.0, 0)],
    108: [(44.5, 1), (37.2, 7), (26.5, 8), (15.5, 9), (5.0, 10), (-7.7, 11), (-30.0, 0)],
    109: [(44.5, 1), (37.2, 7), (26.5, 8), (15.5, 9), (5.0, 10), (-7.7, 11), (-30.0, 0)],
    110: [(44.5, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
    111: [(44.5, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
    112: [(44.5, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
    113: [(44.5, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
    114: [(44.5, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
    115: [(44.5, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
    116: [(37.7, 1), (24.2, 2), (4.0, 3), (-16.2, 4), (-36.5, 0)],
}

# scales (exact game doubles): rows 99-104, 105-109, 110-116
SC_23 = 0.6666666666666666    # 0x528b08 (|value|)
SC_59 = 0.5555555702727522    # 0x528b00 (float32 5/9)
SC_49 = 0.4444444444444444    # 0x528af8

# digit tables for the AK/AM function, rows 105-109 (case 6..10 -> 0x528910..0x5288a0)
AK_NORTH = [
    (19, 1, 29, 2, 39, 3),      # row 105 (0x528910; odd single-digit entries are in the game data as-is)
    (17, 18, 27, 28, 37, 38),   # row 106 (0x5288f4)
    (15, 16, 25, 26, 35, 36),   # row 107 (0x5288d8)
    (13, 14, 23, 24, 33, 34),   # row 108 (0x5288bc)
    (11, 12, 21, 22, 31, 32),   # row 109 (0x5288a0)
]

# letters whose digit function is ported (simple table lookup)
SIMPLE = {'AH', 'AJ', 'AK', 'AM', 'AE', 'AF', 'AG'}


def _trunc(x):
    return int(x)  # values here are positive, so trunc == floor


def compute_grid(lat, lon_w):
    """Return grid string like 'BE 32' / 'AM 77', or a placeholder, or None."""
    if not (LAT_MIN < lat < LAT_MAX):
        return None
    row = _trunc((lat - ROW_SUB) * ROW_MUL)

    if row >= 99:
        return _north_atlantic(row, lon_w)

    # eastern waters -> NS/Baltic handler (not decoded)
    for lo, hi, thr in EASTERN_THRESHOLD:
        if lo <= row < hi and lon_w < thr:
            return '?? (NS/Baltic handler 0x49ca80, not decoded)'

    band, rib_i = divmod(row, 9)
    if band >= len(BAND_BOUND):
        return None
    col = _trunc((BAND_BOUND[band] - lon_w) / BAND_WIDTH[band])
    li = col // 9
    if li >= len(LETTERS[band]) or li > 13:
        return None
    return f"{LETTERS[band][li]} {RIBS[rib_i][col % 9]}"


def _north_atlantic(row, lon_w):
    entries = NS_ROWS.get(row)
    if not entries:
        return None
    if lon_w >= entries[0][0]:
        return None
    for c, (bound, code) in enumerate(entries):
        if code == 0:
            break
        nxt = entries[c + 1][0] if c + 1 < len(entries) else -30.0
        if nxt <= lon_w < bound:
            letter = NS_LETTER_OF[code]
            if letter not in SIMPLE:
                return f"{letter} ?? (complex digit fn, not decoded)"
            value = bound - lon_w
            return f"{letter} {_ns_digit(letter, row, value)}"
    return None


def _ns_digit(letter, row, value):
    if letter in ('AH', 'AJ'):                       # fn 0x497fa0
        if row <= 104:
            return RIBS[row - 99][_trunc(value * SC_23)]
        return RIBS[row - 99][_trunc(value * SC_59)]  # rows 105-107 -> rib6-8
    if letter in ('AK', 'AM'):                       # fn 0x498120
        if row <= 104:
            return RIBS[row - 99][_trunc(value * SC_23)]
        return AK_NORTH[row - 105][_trunc(value * SC_59)]
    # AE/AF/AG (fn 0x497e60), rows 110-116
    return RIBS[row - 110][_trunc(value * SC_49)]


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3:
        lat, lon_w = float(sys.argv[1]), float(sys.argv[2])
        print(compute_grid(lat, lon_w))
    else:
        # self-test against known data points
        tests = [
            (50.74, 15.19, 'BE 31'),   # earlier live read
            (50.50, 13.50, 'BE 32'),   # current game string
            (50.50, 12.625, 'BE 33'),  # frozen cache value
            (51.20, 15.00, 'AM 77'),   # user's pre-crossing quadrat
            (51.20, 13.50, 'AM 78'),
            (51.20, 12.00, 'AM 79'),
            (52.00, 15.00, 'AM 74'),   # row 100 -> rib1
            (52.00, 14.00, 'AM 75'),   # matches watcher log 'AM 75' at crossing
            (55.00, 20.00, 'AL ?? (complex digit fn, not decoded)'),
            (57.00, 36.00, 'AK 19'),   # row 105 north table
            (57.00, 30.00, 'AK 39'),
            (60.50, 30.00, 'AK 31'),   # row 109
        ]
        ok = True
        for lat, lon_w, want in tests:
            got = compute_grid(lat, lon_w)
            flag = 'ok ' if got == want else 'FAIL'
            if got != want:
                ok = False
            print(f"{flag} lat={lat:7.3f} lon_w={lon_w:7.3f} -> {got!r} (want {want!r})")
        sys.exit(0 if ok else 1)