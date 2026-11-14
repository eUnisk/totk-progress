"""Tests for sources/: python -m unittest discover -s tests (from the repository root). No game needed."""
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import savefile  # noqa: E402
from sources import (Capability, MemorySource, Pose, ProgressSnapshot, SaveFileSource, SourceUnavailable,  # noqa: E402
                     yaw_from_rotation, yaw_to_heading)
from sources.memory_reader import LiveError  # noqa: E402


def turn(deg):
    """The Player actor's rotation for a turn of `deg` about y, rows in memory order (row 0 = cos, 0, sin)."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)


class Conventions(unittest.TestCase):
    """The conventions of TotK Recomp's control socket (docs/control.md, runtime/control.c)."""

    def test_heading_matches_control_socket_examples(self):
        # from docs/control.md and the pull request adding it: yaw -136.3 -> heading 316.3, -20.7 -> 200.7
        self.assertEqual(yaw_to_heading(-136.3), 316.3)
        self.assertEqual(yaw_to_heading(-20.7), 200.7)
        self.assertEqual(yaw_to_heading(0), 180.0)     # looking along +z: south on the map
        self.assertEqual(yaw_to_heading(180), 0.0)     # along -z: north
        self.assertIsNone(yaw_to_heading(None))

    def test_yaw_from_rotation_like_control_c(self):
        for deg in (0, 37.8, 90, -136.3, 179):
            self.assertAlmostEqual(yaw_from_rotation(turn(deg)), deg, places=1)
        # read from the game on 1.4.3 while testing the memory reader
        self.assertEqual(yaw_from_rotation((0.790, 0.0, 0.613, 0.0, 1.0, 0.0, -0.613, 0.0, 0.790)), 37.8)

    def test_yaw_unknown_when_not_a_turn(self):
        self.assertIsNone(yaw_from_rotation(None))
        self.assertIsNone(yaw_from_rotation((0.0,) * 9))
        self.assertIsNone(yaw_from_rotation((float('nan'),) * 9))

    def test_pose_json(self):
        j = Pose(1.234, 2, -3.456, yaw=-20.7, scene='MainField', source='test', at=10).to_json()
        self.assertEqual(j, {'x': 1.23, 'y': 2, 'z': -3.46, 'yaw': -20.7, 'heading': 200.7, 'scene': 'MainField',
                             'shrine': None, 'on_map': True, 'source': 'test', 'at': 10})

    def test_shrine_scene(self):
        p = Pose(-20.0, -6.3, 72.6, scene='Dungeon062')
        self.assertEqual((p.shrine, p.on_map), ('Dungeon062', False))
        self.assertEqual(p.to_json()['shrine'], 'Dungeon062')
        # anything else, and an unknown scene, stays on the map
        for scene in (None, 'MainField', 'Dungeon06', 'Dungeon0621', 'xDungeon062'):
            self.assertTrue(Pose(1, 2, 3, scene=scene).on_map, scene)


def make_save(flags, guids, save_pos=None, version_id=0x49E946):
    """A minimal progress.sav: header, (hash, value) table from 0x28 ending at MetaData.SaveTypeHash, then data."""
    entries = list(flags.items())
    if save_pos is not None:
        entries.append((savefile.SAVE_POS_HASH, None))
    table_len = 8 * (len(entries) + 1)
    data_off = savefile.TABLE_START + table_len
    data = b''
    out = bytearray(struct.pack('<II', savefile.HEADER_MAGIC, version_id) + bytes(savefile.TABLE_START - 8))
    for h, v in entries:
        if v is None:   # the save position: offset of three floats in the data part
            v = data_off + len(data)
            data += struct.pack('<3f', *save_pos)
        out += struct.pack('<II', h, v)
    out += struct.pack('<II', savefile.SAVE_TYPE_HASH, data_off + len(data))
    data += b''.join(struct.pack('<Q', g) for g in guids) + struct.pack('<Q', 0)
    return bytes(out + data)


class SaveFileSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / 'slot_02').mkdir()
        self.flags = {savefile.PLAYTIME_HASH: 7760, 0x1234: 1, 0x5678: savefile.STATUS_CLEAR}
        (self.dir / 'slot_02' / 'progress.sav').write_bytes(make_save(self.flags, [0x26A353D06421792], (99.9, 1497.5, 1546.7)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot(self):
        src = SaveFileSource(self.dir)
        self.assertTrue(src.status().available)
        self.assertEqual(src.capabilities, Capability.PROGRESS | Capability.INVENTORY)
        snap = src.progress(2)
        self.assertIsInstance(snap, ProgressSnapshot)
        # plus PlayerStatus.SavePos, whose value is the offset of its three floats, as in savefile.read_table
        self.assertEqual({h: v for h, v in snap.flags.items() if h != savefile.SAVE_POS_HASH}, self.flags)
        self.assertIn(savefile.SAVE_POS_HASH, snap.flags)
        self.assertEqual(snap.guids, frozenset({0x26A353D06421792}))
        self.assertEqual(snap.playtime, 7760)
        self.assertEqual((snap.slot, snap.version_id, snap.source), (2, 0x49E946, 'save'))
        self.assertEqual((snap.save_pos.x, snap.save_pos.y, snap.save_pos.z), (99.9, 1497.5, 1546.7))

    def test_same_as_savefile_parse(self):
        """The snapshot evaluates to what savefile.parse gives for the same file (the old way)."""
        md = {'shrines': [{'completion_hash': 0x5678}, {'completion_hash': 0x9999}],
              'caves': [{'completion_hash': 0x1234}], 'treasure_chests': [{'guid_hash': str(0x26A353D06421792)}]}
        f = self.dir / 'slot_02' / 'progress.sav'
        old = savefile.parse(f, md)
        snap = SaveFileSource(self.dir).progress(2)
        new = savefile.progress_from_table(snap.flags, snap.guids, md)
        self.assertEqual(new['done'], old['done'])
        self.assertEqual(new['found'], old['found'])
        self.assertEqual(new['playtime'], old['playtime'])
        self.assertEqual({'x': snap.save_pos.x, 'y': snap.save_pos.y, 'z': snap.save_pos.z}, old['player'])

    def test_missing(self):
        with self.assertRaises(SourceUnavailable):
            SaveFileSource(None).progress(0)
        self.assertFalse(SaveFileSource(None).status().available)
        with self.assertRaises(savefile.SaveError):
            SaveFileSource(self.dir).progress(3)


class FakeReader:
    def __init__(self, pos=(101.58, 1497.41, 1549.08), rot=turn(-38.5), scene='MainField', fail=None,
                 inventory_fail=None):
        self.pos, self.rot, self._scene, self.fail, self.error = pos, rot, scene, fail, fail
        self.inventory_fail = inventory_fail

    def player(self):
        if self.fail:
            raise LiveError(self.fail)
        return self.pos, self.rot

    def scene(self):
        if self.fail:
            raise LiveError(self.fail)
        return self._scene

    def snapshot(self):
        if self.fail:
            raise LiveError(self.fail)
        return {savefile.PLAYTIME_HASH: 8360}, {1, 2}

    def inventory(self):
        if self.fail or self.inventory_fail:
            raise LiveError(self.fail or self.inventory_fail)
        return {'Armor_001_Head'}, {'Item_Fruit_A': 5}

    def status(self):
        return {'available': not self.fail, 'pid': None if self.fail else 1, 'error': self.fail}


class MemorySourceTest(unittest.TestCase):
    def test_pose(self):
        p = MemorySource(FakeReader()).pose()
        self.assertEqual((p.x, p.yaw, p.heading, p.source), (101.58, -38.5, 218.5, 'memory'))

    def test_pose_without_rotation(self):
        self.assertIsNone(MemorySource(FakeReader(rot=None)).pose().yaw)

    def test_in_shrine(self):
        p = MemorySource(FakeReader(pos=(-20.0, -6.3, 72.6), scene='Dungeon062')).pose()
        self.assertEqual((p.scene, p.shrine, p.on_map), ('Dungeon062', 'Dungeon062', False))
        # a shrine's own coordinates can be near 0: that is not the title screen
        p = MemorySource(FakeReader(pos=(0.2, -6.3, 0.3), scene='Dungeon062')).pose()
        self.assertEqual(p.shrine, 'Dungeon062')

    def test_title_screen_is_no_save(self):
        with self.assertRaisesRegex(SourceUnavailable, 'No save loaded'):
            MemorySource(FakeReader(pos=(0.0, 0.0, 0.0))).pose()
        self.assertFalse(MemorySource(FakeReader(pos=(0.0, 0.0, 0.0))).state().in_world)

    def test_game_not_running(self):
        src = MemorySource(FakeReader(fail='totk.exe is not running'))
        for call in (src.pose, src.progress):
            with self.assertRaisesRegex(SourceUnavailable, 'not running'):
                call()
        st = src.status()
        self.assertFalse(st.available)
        self.assertEqual(st.to_json()['capabilities'], ['pose', 'state', 'progress', 'inventory'])
        self.assertTrue(st.to_json()['experimental'])

    def test_progress(self):
        snap = MemorySource(FakeReader()).progress()
        self.assertEqual((snap.playtime, snap.guids, snap.slot, snap.source), (8360, frozenset({1, 2}), None, 'memory'))
        self.assertEqual(snap.inventory, (frozenset({'Armor_001_Head'}), {'Item_Fruit_A': 5}))

    def test_progress_without_inventory(self):
        # the flags still come through; the app then takes the outfits from the latest save
        snap = MemorySource(FakeReader(inventory_fail='array missing')).progress()
        self.assertEqual((snap.playtime, snap.inventory), (8360, None))


if __name__ == '__main__':
    unittest.main()
