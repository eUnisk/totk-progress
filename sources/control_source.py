"""The running game through a control socket: TotK Recomp's own (docs/control.md there) or totk-live-bridge's.

Both speak one protocol: TCP on 127.0.0.1, one request per line, one JSON reply per line, `{"ok":true,"data":{...}}`
or `{"ok":false,"error":"..."}`. The official socket answers `help`, `pos` and `state`; the bridge
(github.com/eUnisk/totk-live-bridge, a stopgap that reads the game's memory) answers those too, plus `x-scene`,
`x-flags` and `x-inventory`. What a socket can answer comes from its `help`, never from which one it is, so the app
moves to the official socket by itself for whatever that answers.

Before any data is used, `help` is checked:
1. a bridge (its `help` has a `bridge` object) must speak PROTOCOL, the reply shapes this client reads;
2. a bridge must say it reads this game build (`build_ok`); otherwise its error is shown and nothing is read;
3. the first flags must look like a game's: the play time flag present, and thousands of flags.
Any of these failing makes the source unavailable, with the reason; the app then keeps reading the saves.
"""
import base64
import json
import socket
import struct
import threading
import time

from .model import (PLAYTIME_HASH, Capability, GameSource, GameState, Pose, ProgressSnapshot, SourceStatus,
                    SourceUnavailable)

HOST = '127.0.0.1'
OFFICIAL_PORT, BRIDGE_PORT = 7777, 7778
PROTOCOL = 1             # the totk-live-bridge protocol this client reads
MIN_FLAGS = 1000         # a running game has ~30 000; fewer means the reply isn't what we think
RETRY_S, RETRY_MAX_S = 2.0, 15.0   # wait between tries while nothing listens, doubling up to the max
CONNECT_S = 0.3          # a local socket answers at once; Windows otherwise takes ~2 s to refuse a closed port
BRIDGE_URL = 'https://github.com/eUnisk/totk-live-bridge'

# command -> what it gives
COMMAND_CAPS = {'pos': Capability.POSE, 'state': Capability.STATE, 'x-flags': Capability.PROGRESS,
                'x-inventory': Capability.INVENTORY}


class ControlSource(GameSource):
    experimental = False

    def __init__(self, port, timeout=3.0, host=HOST):
        self.host, self.port, self.timeout = host, port, timeout
        self.lock = threading.Lock()
        self.sock = self.rfile = None
        self.next_try = 0.0
        self.retry_s = RETRY_S
        self.commands = None      # set of command names, from help
        self.bridge = None        # the bridge object of help, or None for the official socket
        self.error = 'not connected yet'
        self.checked_flags = False
        self.help_at = 0.0        # when the bridge's help was last read

    # ---- the connection
    @property
    def name(self):
        return 'bridge' if self.bridge else 'official' if self.commands is not None else f'control:{self.port}'

    @property
    def capabilities(self):
        caps = Capability.NONE
        for cmd, cap in COMMAND_CAPS.items():
            if self.commands and cmd in self.commands:
                caps |= cap
        return caps

    def _close(self):
        for f in (self.rfile, self.sock):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        self.sock = self.rfile = None
        self.commands = self.bridge = None
        self.checked_flags = False

    def _send(self, cmd):
        self.sock.sendall(cmd.encode('ascii') + b'\n')
        line = self.rfile.readline()
        if not line:
            raise OSError('connection closed')
        return json.loads(line)

    def _connect(self):
        if self.sock:
            return
        now = time.monotonic()
        if now < self.next_try:
            raise SourceUnavailable(self.error)
        self.next_try = now + self.retry_s
        self.retry_s = min(self.retry_s * 2, RETRY_MAX_S)
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=CONNECT_S)
        except OSError as e:
            self.sock = None
            self.error = f'nothing answers on {self.host}:{self.port}'
            raise SourceUnavailable(self.error) from e
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # small requests: no 200 ms waits
            self.sock.settimeout(self.timeout)
            self.rfile = self.sock.makefile('rb')
            reply = self._send('help')
        except (OSError, ValueError) as e:
            self._close()
            self.error = f'{self.host}:{self.port}: {e}'
            raise SourceUnavailable(self.error) from e
        data = reply.get('data') if reply.get('ok') else None
        if not isinstance(data, dict) or not isinstance(data.get('commands'), list):
            self._close()
            self.error = f'{self.host}:{self.port} is not a TotK control socket'
            raise SourceUnavailable(self.error)
        self.commands = {c.get('name') for c in data['commands'] if isinstance(c, dict)}
        self.retry_s = RETRY_S
        self.bridge = data.get('bridge') if isinstance(data.get('bridge'), dict) else None

    def _check_bridge(self):
        """Checks 1 and 2 of the module doc, on the latest help (the game may have started since)."""
        b = self.bridge
        if b is None:
            return
        proto = b.get('protocol')
        if proto != PROTOCOL:
            older = isinstance(proto, int) and proto < PROTOCOL
            raise SourceUnavailable(
                f"totk-live-bridge {b.get('version')} speaks protocol {proto}, this app reads protocol {PROTOCOL}: "
                f"update {'totk-live-bridge' if older else 'this app'} ({BRIDGE_URL})")
        if b.get('build_ok') is False:
            raise SourceUnavailable(b.get('error') or f"unsupported game build: the bridge reads {b.get('supported')}")
        if not b.get('available'):
            raise SourceUnavailable(b.get('error') or 'the game is not running')

    def _refresh_bridge(self, force=False):
        """Re-read the bridge's help (at most once a second: the game may start, stop or change) and check it."""
        now = time.monotonic()
        if force or now - self.help_at >= 1.0:
            self.bridge = self._send('help')['data'].get('bridge') or self.bridge
            self.help_at = now
        self._check_bridge()

    def ask(self, cmd):
        """The data of one command, or SourceUnavailable with the socket's error (or why it couldn't be asked)."""
        with self.lock:
            self._connect()
            if cmd not in self.commands:
                raise SourceUnavailable(f'{self.name} socket has no {cmd}')
            try:
                if self.bridge is not None:
                    self._refresh_bridge()
                reply = self._send(cmd)
            except (OSError, ValueError, KeyError, TypeError) as e:
                self._close()
                self.error = f'{self.host}:{self.port}: {e}'
                raise SourceUnavailable(self.error) from e
            except SourceUnavailable as e:
                self.error = str(e)
                raise
            if not reply.get('ok'):
                self.error = str(reply.get('error') or f'{cmd} failed')
                raise SourceUnavailable(self.error)
            self.error = None
            return reply.get('data') or {}

    # ---- GameSource
    def status(self):
        try:
            with self.lock:
                self._connect()
                if self.bridge is not None:
                    self._refresh_bridge(force=True)
            available, error = True, None
        except (OSError, ValueError, KeyError, TypeError) as e:
            with self.lock:
                self._close()
            available, error = False, str(e)
        except SourceUnavailable as e:
            available, error = False, str(e)
        detail = {'port': self.port, 'experimental': self.bridge is not None}
        if self.bridge:
            detail.update(version=self.bridge.get('version'), protocol=self.bridge.get('protocol'),
                          supported=self.bridge.get('supported'))
        return SourceStatus(self.name, available, self.capabilities, error, detail)

    def scene(self):
        """'MainField' or the shrine Link is in, when the socket tells (x-scene); else None."""
        with self.lock:
            self._connect()
        if 'x-scene' not in self.commands:
            return None
        return self.ask('x-scene').get('scene')

    def pose(self):
        d = self.ask('pos')
        try:
            return Pose(float(d['x']), float(d['y']), float(d['z']), d.get('yaw'), source=self.name)
        except (KeyError, TypeError, ValueError) as e:
            raise SourceUnavailable(f'{self.name}: unexpected pos reply ({e})') from e

    def state(self):
        d = self.ask('state')
        return GameState(bool(d.get('in_world')), game=d.get('game'), recomp=d.get('recomp'),
                         world_s=d.get('world_s'), source=self.name)

    def progress(self, slot=None):
        if slot not in (None, 'live'):
            raise SourceUnavailable('the running game has no save slots')
        d = self.ask('x-flags')
        try:
            flags = dict(struct.iter_unpack('<II', base64.b64decode(d['flags'])))
            guids = frozenset(g for (g,) in struct.iter_unpack('<Q', base64.b64decode(d['guids'])))
        except (KeyError, TypeError, ValueError, struct.error) as e:
            raise SourceUnavailable(f'{self.name}: unexpected x-flags reply ({e})') from e
        if len(flags) != d.get('count', len(flags)) or len(guids) != d.get('guid_count', len(guids)):
            raise SourceUnavailable(f'{self.name}: x-flags reply cut short')
        # check 3: the first flags must look like a game's
        if not self.checked_flags:
            if PLAYTIME_HASH not in flags or len(flags) < MIN_FLAGS:
                self.error = (f'{self.name}: the flags read ({len(flags)}) do not look like the game\'s; '
                              f'live data turned off')
                raise SourceUnavailable(self.error)
            self.checked_flags = True
        inventory = None
        if 'x-inventory' in self.commands:
            try:
                inv = self.ask('x-inventory')
                inventory = (frozenset(inv['armor']), {k: int(v) for k, v in inv['materials'].items()})
            except (SourceUnavailable, KeyError, TypeError, ValueError, AttributeError):
                inventory = None   # the app takes the outfits from the latest save then
        return ProgressSnapshot(flags, guids, source=self.name, inventory=inventory)

    def close(self):
        with self.lock:
            self._close()


class LiveSource(GameSource):
    """The running game, from whichever control sockets answer: for each kind of data the first socket that has it.
    The official socket comes first, so it takes over from the bridge for each command it answers."""
    name = 'live'

    def __init__(self, sources=None):
        self.sources = sources if sources is not None else [ControlSource(OFFICIAL_PORT), ControlSource(BRIDGE_PORT)]

    @property
    def capabilities(self):
        caps = Capability.NONE
        for s in self.sources:
            caps |= s.capabilities
        return caps

    def _first(self, cap, call):
        errors = []
        for s in self.sources:
            if s.commands is not None and cap not in s.capabilities:
                continue   # connected and it doesn't have that
            try:
                return call(s)
            except SourceUnavailable as e:
                errors.append(str(e))
        raise SourceUnavailable(next((e for e in errors if not e.startswith('nothing answers')), None)
                                or (errors[-1] if errors else 'no live source'))

    def status(self):
        sts = [s.status() for s in self.sources]
        by_name = {st.name: st.to_json() for st in sts}
        ok = [st for st in sts if st.available]
        progress = next((st for st in ok if Capability.PROGRESS in st.capabilities), None)
        error = None
        if not progress:
            error = next((st.error for st in sts if st.error and not st.error.startswith('nothing answers')),
                         None) or 'no live data: start the game and totk-live-bridge'
        caps = Capability.NONE
        for st in ok:
            caps |= st.capabilities
        return SourceStatus(self.name, progress is not None, caps, error,
                            {'sources': by_name, 'experimental': any(s.get('experimental') for s in by_name.values()),
                             'supported': (progress.detail.get('supported') if progress else None)})

    def pose(self):
        pose = self._first(Capability.POSE, lambda s: s.pose())
        scene = None
        for s in self.sources:   # the official socket has no scene yet: a socket that has it tells
            try:
                scene = s.scene()
            except SourceUnavailable:
                continue
            if scene:
                break
        if scene:
            pose = Pose(pose.x, pose.y, pose.z, pose.yaw, scene=scene, source=pose.source, at=pose.at)
        return pose

    def state(self):
        return self._first(Capability.STATE, lambda s: s.state())

    def progress(self, slot=None):
        return self._first(Capability.PROGRESS, lambda s: s.progress(slot))

    def close(self):
        for s in self.sources:
            s.close()


def start_bridge(port=BRIDGE_PORT):
    """Serve totk-live-bridge in this process when it is installed and nothing listens on its port yet.
    Returns a short note for the log."""
    try:
        from totk_live_bridge import BridgeServer
    except ImportError:
        return f'totk-live-bridge not installed ({BRIDGE_URL}); live data only from a socket already running'
    try:
        server = BridgeServer(port=port).start()
    except OSError:
        return f'port {port} already in use: using the totk-live-bridge (or other socket) there'
    import totk_live_bridge
    return f'totk-live-bridge {totk_live_bridge.__version__} serving on {HOST}:{server.port}'
