"""Tests for sources/: python -m unittest discover -s tests (from the repository root). No game needed."""
import base64
import json
import math
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import savefile  # noqa: E402
from sources import (Capability, ControlSource, LiveSource, Pose, ProgressSnapshot, SaveFileSource,  # noqa: E402
                     SourceUnavailable, yaw_from_rotation, yaw_to_heading)


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


class FakeSocket:
    """A control socket on a free port: answers each request line with handler(command) (a reply dict)."""

    def __init__(self, handler):
        self.handler = handler
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._conn, args=(conn,), daemon=True).start()

    def _conn(self, conn):
        try:
            with conn, conn.makefile('rb') as f:
                for line in f:
                    conn.sendall(json.dumps(self.handler(line.decode().strip())).encode() + b'\n')
        except OSError:
            pass

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


FLAGS = {savefile.PLAYTIME_HASH: 8360, **{i: 1 for i in range(1, 2000)}}


def ok(data):
    return {'ok': True, 'data': data}


def unknown(cmd):
    return {'ok': False, 'error': f'unknown command: {cmd}'}


def official(cmd):
    """TotK Recomp's control socket, as its docs/control.md describes it."""
    if cmd == 'help':
        return ok({'commands': [{'name': n, 'args': '', 'help': ''} for n in ('help', 'pos', 'state')]})
    if cmd == 'pos':
        return ok({'x': 843.4, 'y': 119.1, 'z': 849.6, 'yaw': -136.3, 'heading': 316.3, 'place': 'totk:1 ...'})
    if cmd == 'state':
        return ok({'game': '1.4.3', 'recomp': '0.5.0', 'in_world': True})
    return unknown(cmd)


def bridge(protocol=1, build_ok=True, available=True, error=None, flags=FLAGS, scene='MainField'):
    """totk-live-bridge."""
    def handler(cmd):
        if cmd == 'help':
            names = ['help', 'pos', 'state', 'x-scene', 'x-flags', 'x-inventory']
            return ok({'commands': [{'name': n, 'args': '', 'help': ''} for n in names],
                       'bridge': {'name': 'totk-live-bridge', 'version': '0.1.0', 'protocol': protocol,
                                  'supported': 'TotK 1.4.3 / TotK Recomp 0.4.0', 'available': available,
                                  'build_ok': build_ok, 'error': error}})
        if cmd == 'pos':
            return ok({'x': 101.58, 'y': 1497.41, 'z': 1549.08, 'yaw': -38.5, 'heading': 218.5, 'place': '...'})
        if cmd == 'x-scene':
            return ok({'scene': scene, 'shrine': scene != 'MainField'})
        if cmd == 'x-flags':
            packed = b''.join(struct.pack('<II', h, v) for h, v in flags.items())
            return ok({'count': len(flags), 'flags': base64.b64encode(packed).decode(), 'guid_count': 2,
                       'guids': base64.b64encode(struct.pack('<2Q', 1, 2)).decode()})
        if cmd == 'x-inventory':
            return ok({'armor': ['Armor_001_Head'], 'materials': {'Item_Fruit_A': 5}})
        return unknown(cmd)
    return handler


class SocketCase(unittest.TestCase):
    def setUp(self):
        self.servers, self.sources = [], []

    def tearDown(self):
        for s in self.sources:
            s.close()
        for s in self.servers:
            s.close()

    def serve(self, handler):
        srv = FakeSocket(handler)
        self.servers.append(srv)
        return self.source(srv.port)

    def source(self, port):
        src = ControlSource(port)
        self.sources.append(src)
        return src


class ControlSourceTest(SocketCase):
    def test_official_socket(self):
        src = self.serve(official)
        st = src.status()
        self.assertTrue(st.available)
        self.assertEqual((st.name, st.to_json()['capabilities'], st.to_json()['experimental']),
                         ('official', ['pose', 'state'], False))
        p = src.pose()
        self.assertEqual((p.x, p.yaw, p.heading, p.source), (843.4, -136.3, 316.3, 'official'))
        self.assertEqual(src.state().recomp, '0.5.0')
        with self.assertRaisesRegex(SourceUnavailable, 'no x-flags'):
            src.progress()

    def test_bridge(self):
        src = self.serve(bridge())
        st = src.status().to_json()
        self.assertEqual((st['name'], st['capabilities'], st['experimental'], st['protocol']),
                         ('bridge', ['pose', 'state', 'progress', 'inventory'], True, 1))
        snap = src.progress()
        self.assertEqual((snap.playtime, snap.guids, snap.source), (8360, frozenset({1, 2}), 'bridge'))
        self.assertEqual(snap.inventory, (frozenset({'Armor_001_Head'}), {'Item_Fruit_A': 5}))
        self.assertEqual(src.scene(), 'MainField')

    def test_error_passes_through(self):
        src = self.serve(lambda cmd: bridge()(cmd) if cmd == 'help' else {'ok': False, 'error': 'No save loaded'})
        with self.assertRaisesRegex(SourceUnavailable, '^No save loaded$'):
            src.pose()

    def test_protocol_mismatch(self):
        src = self.serve(bridge(protocol=2))
        st = src.status()
        self.assertFalse(st.available)
        self.assertIn('protocol 2, this app reads protocol 1: update this app', st.error)
        with self.assertRaisesRegex(SourceUnavailable, 'protocol 2'):
            src.progress()
        self.assertIn('update totk-live-bridge', self.serve(bridge(protocol=0)).status().error)

    def test_unsupported_build(self):
        src = self.serve(bridge(build_ok=False, available=False, error='unsupported game build (table 0 vtable 0x1)'))
        self.assertIn('unsupported game build', src.status().error)
        with self.assertRaisesRegex(SourceUnavailable, 'unsupported game build'):
            src.pose()

    def test_game_not_running(self):
        src = self.serve(bridge(build_ok=None, available=False, error='totk.exe is not running'))
        self.assertEqual(src.status().error, 'totk.exe is not running')

    def test_flags_that_dont_look_right(self):
        src = self.serve(bridge(flags={1: 1, 2: 2}))
        with self.assertRaisesRegex(SourceUnavailable, 'do not look like'):
            src.progress()

    def test_not_a_control_socket(self):
        src = self.serve(lambda cmd: {'hello': 'world'})
        self.assertIn('not a TotK control socket', src.status().error)

    def test_nothing_listening(self):
        st = self.source(free_port()).status()
        self.assertFalse(st.available)
        self.assertIn('nothing answers', st.error)

    def test_reconnects(self):
        srv = FakeSocket(bridge())
        src = self.source(srv.port)
        self.assertEqual(src.pose().x, 101.58)
        srv.close()   # the bridge stops ...
        with self.assertRaises(SourceUnavailable):
            src.pose()
        srv2 = FakeSocket(bridge())   # ... and starts again
        self.servers.append(srv2)
        src.port, src.next_try = srv2.port, 0
        self.assertEqual(src.pose().x, 101.58)


class LiveSourceTest(SocketCase):
    def test_official_first_bridge_for_the_rest(self):
        live = LiveSource([self.serve(official), self.serve(bridge(scene='Dungeon062'))])
        p = live.pose()
        self.assertEqual((p.x, p.source, p.shrine), (843.4, 'official', 'Dungeon062'))   # the scene from the bridge
        self.assertEqual(live.progress().source, 'bridge')
        st = live.status().to_json()
        self.assertTrue(st['available'])
        self.assertEqual(set(st['sources']), {'official', 'bridge'})

    def test_bridge_alone(self):
        live = LiveSource([self.source(free_port()), self.serve(bridge())])
        self.assertEqual(live.pose().source, 'bridge')
        self.assertTrue(live.status().available)

    def test_nothing_running(self):
        live = LiveSource([self.source(free_port()), self.source(free_port())])
        st = live.status()
        self.assertFalse(st.available)
        self.assertIn('totk-live-bridge', st.error)
        with self.assertRaises(SourceUnavailable):
            live.progress()

    def test_official_alone_has_no_progress(self):
        live = LiveSource([self.serve(official)])
        self.assertFalse(live.status().available)
        self.assertEqual(live.pose().source, 'official')

    def test_bridge_problem_is_shown(self):
        live = LiveSource([self.source(free_port()), self.serve(bridge(protocol=2))])
        self.assertIn('protocol 2', live.status().error)


if __name__ == '__main__':
    unittest.main()
