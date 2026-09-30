#!/usr/bin/env python3
"""Tests for collectors/crew.py roster parsing (stories #12, #22)."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'collectors'))

from crew import (parse_roster, _valid_name, ROSTER_STRIDE, COMPARTMENTS,
                 parse_slots, apply_slot_overrides, apply_position_overrides,
                 POSITION_COMPARTMENTS, POSITION_SLOT_COUNTS,
                 SLOT_ARRAY_BASE)


def make_entry(name=b'', index=0, nrc=-1, rank=0, morale=0.7, fatigue=0.2):
    e = bytearray(ROSTER_STRIDE)
    if name:
        e[0:len(name)] = name
    struct.pack_into('<I', e, 0x34, index)
    struct.pack_into('<i', e, 0x38, nrc)
    struct.pack_into('<I', e, 0x44, rank)
    struct.pack_into('<f', e, 0x50, morale)
    struct.pack_into('<f', e, 0x54, fatigue)
    return bytes(e)


def make_table(entries):
    """Build a roster table from {position: entry} — empty positions are
    blank entries (nrc=-1, no name)."""
    table = {}
    for pos, entry in entries.items():
        table[pos] = entry
    count = max(table) + 1 if table else 0
    return b''.join(table.get(p, make_entry()) for p in range(count))


class TestValidName(unittest.TestCase):
    def test_normal_name(self):
        self.assertEqual(_valid_name(b'Adolf Kessler\x00'), 'Adolf Kessler')

    def test_hyphenated(self):
        self.assertEqual(_valid_name(b'Franz Linke-Stier\x00'), 'Franz Linke-Stier')

    def test_empty(self):
        self.assertEqual(_valid_name(b'\x00\x00\x00'), '')

    def test_too_short(self):
        self.assertEqual(_valid_name(b'Ab\x00'), '')

    def test_non_ascii(self):
        self.assertEqual(_valid_name(b'\xff\xfe\xfd\xfc\x00'), '')

    def test_mostly_symbols(self):
        self.assertEqual(_valid_name(b'@#$%&*(!\x00'), '')


class TestParseRoster(unittest.TestCase):
    def test_live_entry(self):
        data = make_entry(b'Adolf Kessler', index=8, nrc=7, rank=1,
                          morale=0.7, fatigue=0.25)
        crew = parse_roster(data)
        self.assertEqual(len(crew), 1)
        c = crew[0]
        self.assertEqual(c['name'], 'Adolf Kessler')
        self.assertEqual(c['index'], 8)
        self.assertEqual(c['position'], 0)
        self.assertEqual(c['compartment_id'], 7)
        self.assertEqual(c['compartment'], 'Stern Quarters')
        self.assertEqual(c['rank_tier'], 1)
        self.assertAlmostEqual(c['fatigue'], 0.25, places=2)
        self.assertAlmostEqual(c['endurance'], 0.75, places=2)

    def test_stale_entry_skipped(self):
        data = make_entry(b'Adolf Kessler', nrc=-1)
        self.assertEqual(parse_roster(data), [])

    def test_unnamed_entry_skipped(self):
        data = make_entry(b'', nrc=3)
        self.assertEqual(parse_roster(data), [])

    def test_garbage_name_skipped(self):
        data = make_entry(b'\x01\x02\x03\x04\x05\x06', nrc=3)
        self.assertEqual(parse_roster(data), [])

    def test_out_of_range_floats_skipped(self):
        data = make_entry(b'Adolf Kessler', nrc=7, morale=5.0)
        self.assertEqual(parse_roster(data), [])
        data = make_entry(b'Adolf Kessler', nrc=7, fatigue=-0.5)
        self.assertEqual(parse_roster(data), [])

    def test_duplicate_names_deduped(self):
        data = (make_entry(b'Adolf Kessler', nrc=7) +
                make_entry(b'Adolf Kessler', nrc=5))
        crew = parse_roster(data)
        self.assertEqual(len(crew), 1)
        self.assertEqual(crew[0]['compartment_id'], 7)

    def test_multiple_entries(self):
        data = (make_entry(b'Adolf Kessler', index=8, nrc=7) +
                make_entry(b'Werner Bretzler', index=0, nrc=6) +
                make_entry(b'Wolfgang Eisenholtz', index=5, nrc=0) +
                make_entry(b'Franz Linke-Stier', index=13, nrc=2))
        crew = parse_roster(data)
        self.assertEqual(len(crew), 4)
        by_name = {c['name']: c for c in crew}
        self.assertEqual(by_name['Werner Bretzler']['compartment'],
                         'Crew Quarters')
        self.assertEqual(by_name['Wolfgang Eisenholtz']['compartment'],
                         'Conning Tower')

    def test_unknown_compartment_id(self):
        data = make_entry(b'Adolf Kessler', nrc=42)
        crew = parse_roster(data)
        self.assertEqual(crew[0]['compartment'], 'Unknown (42)')
        self.assertEqual(crew[0]['compartment_id'], 42)

    def test_endurance_is_one_minus_fatigue(self):
        data = make_entry(b'Adolf Kessler', nrc=0, fatigue=0.652)
        c = parse_roster(data)[0]
        self.assertAlmostEqual(c['endurance'], 0.348, places=2)

    def test_all_compartments_have_labels(self):
        for cid in range(12):
            self.assertIn(cid, COMPARTMENTS)


class TestApplyPositionOverrides(unittest.TestCase):
    """Issue #22: the table position is the crewman's global slot number
    and is authoritative over a stale nrc."""

    def _crew_at(self, pos, name=b'Adolf Kessler', nrc=-1):
        crew = parse_roster(make_table({pos: make_entry(name, nrc=nrc)}))
        self.assertEqual(len(crew), 1)
        return apply_position_overrides(crew)[0]

    def test_swap_displaced_crewman_corrected(self):
        # Gabelmann case (observed live): displaced off a bridge slot into
        # a quarters bunk at position 68, nrc stayed 0 (bridge) — stale.
        c = self._crew_at(68, b'Heinrich Gabelmann', nrc=0)
        self.assertEqual(c['compartment_id'], 6)
        self.assertEqual(c['compartment'], 'Crew Quarters')
        self.assertEqual(c['station'], 'crew_quarters')
        self.assertFalse(c['on_duty'])
        self.assertEqual(c['position_source'], 'position')

    def test_stale_nrc_at_stern_torpedo(self):
        # Haas case (observed live): position 101 (stern torpedo), nrc 6.
        c = self._crew_at(101, b'Walther Haas', nrc=6)
        self.assertEqual(c['compartment'], 'Stern Torpedo Room')
        self.assertEqual(c['station'], 'stern_torpedo')
        self.assertTrue(c['on_duty'])

    def test_gun_flak_damage_control_positions(self):
        self.assertEqual(self._crew_at(18, nrc=9)['compartment'], 'Deck Gun')
        self.assertEqual(self._crew_at(18, nrc=9)['station'], 'deck_gun')
        self.assertEqual(self._crew_at(21, nrc=10)['compartment'], 'Flak Gun')
        self.assertEqual(self._crew_at(21, nrc=10)['station'], 'flak')
        self.assertEqual(self._crew_at(117, nrc=11)['compartment'],
                         'Damage Control Team')
        self.assertEqual(self._crew_at(117, nrc=11)['station'],
                         'damage_control')

    def test_position_agrees_with_fresh_nrc(self):
        c = self._crew_at(34, nrc=3)
        self.assertEqual(c['compartment'], 'Diesel Engine Room')
        self.assertTrue(c['on_duty'])

    def test_unknown_position_falls_back_to_nrc(self):
        c = self._crew_at(13, nrc=3)  # position 13 is not a crew slot
        self.assertEqual(c['compartment_id'], 3)
        self.assertEqual(c['position_source'], 'nrc')

    def test_position_map_matches_verified_slot_counts(self):
        # User-verified in-game slot counts per compartment (94 total).
        counts = {}
        for cid in POSITION_COMPARTMENTS.values():
            counts[cid] = counts.get(cid, 0) + 1
        self.assertEqual(sum(counts.values()), 94)
        for cid, expected in POSITION_SLOT_COUNTS.items():
            self.assertEqual(counts.get(cid, 0), expected,
                             f"compartment {cid} ({COMPARTMENTS[cid]})")

    def test_radio_station_position(self):
        # Position 30 = radio station (radio room, compartment 1).
        c = self._crew_at(30, b'Engelbert Duhne', nrc=1)
        self.assertEqual(c['compartment'], 'Radio/Sonar Room')
        self.assertTrue(c['on_duty'])


def make_slot_node(name=b''):
    n = bytearray(0x64)
    if name:
        n[0:len(name)] = name
    return bytes(n)


class TestParseSlots(unittest.TestCase):
    def test_occupied_and_vacant(self):
        data = (make_slot_node(b'Adolf Kessler') +
                make_slot_node() +
                make_slot_node(b'Werner Bretzler'))
        occ = parse_slots(data)
        self.assertEqual(occ, {'Adolf Kessler': 0x546284,
                               'Werner Bretzler': 0x546284 + 2 * 0x64})

    def test_ent_placeholder_filtered(self):
        data = make_slot_node(b'ent')
        self.assertEqual(parse_slots(data), {})

    def test_garbage_filtered(self):
        data = make_slot_node(b'\x01\x02\x03\x04\x05')
        self.assertEqual(parse_slots(data), {})


class TestApplySlotOverrides(unittest.TestCase):
    def _crew(self):
        # Kessler in stern quarters (position 85), Bretzler in a bow
        # quarters bunk (position 70).
        return apply_position_overrides(parse_roster(make_table({
            85: make_entry(b'Adolf Kessler', nrc=7),
            70: make_entry(b'Werner Bretzler', nrc=6),
        })))

    def test_labeled_slot_overrides_position(self):
        crew = self._crew()
        # Bretzler actually at the radio slot node; position/nrc stale.
        occ = {'Werner Bretzler': 0x546540}
        apply_slot_overrides(crew, occ)
        by_name = {c['name']: c for c in crew}
        c = by_name['Werner Bretzler']
        self.assertEqual(c['slot_source'], 'slot')
        self.assertEqual(c['compartment'], 'Radio/Sonar Room')
        self.assertEqual(c['compartment_id'], 1)
        self.assertEqual(c['station'], 'radio_room')
        self.assertTrue(c['on_duty'])

    def test_unlabeled_slot_flagged_position_kept(self):
        crew = self._crew()
        occ = {'Adolf Kessler': 0x5462e8 + 0x64 * 20}  # unlabeled node
        apply_slot_overrides(crew, occ)
        by_name = {c['name']: c for c in crew}
        c = by_name['Adolf Kessler']
        self.assertEqual(c['slot_source'], 'slot-unlabeled')
        self.assertEqual(c['compartment'], 'Stern Quarters')  # kept

    def test_crew_not_in_slots_gets_position(self):
        crew = self._crew()
        apply_slot_overrides(crew, {})
        by_name = {c['name']: c for c in crew}
        self.assertEqual(by_name['Adolf Kessler']['slot_source'], 'nrc')
        self.assertEqual(by_name['Adolf Kessler']['compartment'],
                         'Stern Quarters')

    def test_sonar_slot_station(self):
        crew = apply_position_overrides(parse_roster(
            make_table({29: make_entry(b'Arno Popper', nrc=1)})))
        apply_slot_overrides(crew, {'Arno Popper': 0x5465a4})
        self.assertEqual(crew[0]['compartment'], 'Radio/Sonar Room')
        self.assertEqual(crew[0]['station'], 'control_room')
        self.assertTrue(crew[0]['on_duty'])

    def test_gun_and_flak_slots(self):
        crew = apply_position_overrides(parse_roster(make_table({
            18: make_entry(b'Jurgen Mannesmann', nrc=9),
            21: make_entry(b'Hans-Bernhard Wintermeyer', nrc=10),
        })))
        apply_slot_overrides(crew, {
            'Jurgen Mannesmann': 0x5467fc,
            'Hans-Bernhard Wintermeyer': 0x546928,
        })
        self.assertEqual(crew[0]['compartment'], 'Deck Gun')
        self.assertEqual(crew[0]['station'], 'deck_gun')
        self.assertEqual(crew[1]['compartment'], 'Flak Gun')
        self.assertEqual(crew[1]['station'], 'flak')


class TestUIFields(unittest.TestCase):
    def test_parse_roster_ui_fields(self):
        crew = parse_roster(make_entry(b'Adolf Kessler', nrc=7, rank=2))
        c = crew[0]
        self.assertEqual(c['station'], 'crew_quarters')
        self.assertFalse(c['on_duty'])
        self.assertTrue(c['officer'])
        self.assertEqual(c['rank'], 'Officer')
        self.assertIsNone(c['specialty'])

    def test_on_duty_at_station(self):
        crew = parse_roster(make_entry(b'Gotthard Bekker', nrc=8, rank=1))
        c = crew[0]
        self.assertEqual(c['station'], 'stern_torpedo')
        self.assertTrue(c['on_duty'])
        self.assertEqual(c['rank'], 'Petty Officer')

    def test_quarters_resting_radio_station_working(self):
        # nrc=6 alone -> bow quarters (resting)
        crew = apply_position_overrides(parse_roster(
            make_table({70: make_entry(b'Werner Bretzler', nrc=6)})))
        apply_slot_overrides(crew, {})
        self.assertEqual(crew[0]['station'], 'crew_quarters')
        self.assertFalse(crew[0]['on_duty'])
        # same crewman in the radio slot node -> radio station (working)
        crew = apply_position_overrides(parse_roster(
            make_table({70: make_entry(b'Werner Bretzler', nrc=6)})))
        apply_slot_overrides(crew, {'Werner Bretzler': 0x546540})
        self.assertEqual(crew[0]['station'], 'radio_room')
        self.assertTrue(crew[0]['on_duty'])

    def test_slot_override_updates_station(self):
        # nrc says diesel (3) but the crewman sits in the sonar slot node
        crew = parse_roster(make_entry(b'Harald Schubert', nrc=3))
        self.assertEqual(crew[0]['station'], 'engine_room')
        apply_slot_overrides(crew, {'Harald Schubert': 0x5465a4})
        self.assertEqual(crew[0]['compartment'], 'Radio/Sonar Room')
        self.assertEqual(crew[0]['station'], 'control_room')
        self.assertTrue(crew[0]['on_duty'])


if __name__ == '__main__':
    unittest.main()