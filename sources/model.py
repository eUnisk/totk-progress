"""The data every game source hands to the rest of the app, whichever way it got it.

Field names and conventions follow the control socket of TotK Recomp (`docs/control.md` there, the same protocol as
Savage Wind's control endpoint), so a source reading that socket passes its answers through unchanged:

- world coordinates `x`, `y`, `z` with y up, the same as `PlayerStatus.SavePos` in a save;
- `yaw`: the way Link faces in degrees, 0 along +z, 90 along +x, in (-180, 180];
- `heading`: the same direction as a map bearing, clockwise from north (-z), in [0, 360).

Times are Unix seconds (float). A value a source doesn't know is None, never a guess.
"""
import enum
import math
import re
import time
from dataclasses import dataclass, field


PLAYTIME_HASH = 0xE573F564   # MetaData.PlayTime (UInt, seconds), also in savefile


class Capability(enum.Flag):
    """What a source can answer. Sources say this up front so the app picks one per kind of data."""
    NONE = 0
    POSE = enum.auto()       # Link's live position (and facing, when known)
    STATE = enum.auto()      # versions, whether a save is loaded, game time
    PROGRESS = enum.auto()   # the GameData flags and the set GUIDs that say what is done


class SourceUnavailable(Exception):
    """The source can't answer right now (game not running, nothing loaded, unsupported build, ...)."""


def yaw_to_heading(yaw):
    return None if yaw is None else round((180.0 - yaw) % 360.0, 1)


def yaw_from_rotation(rot):
    """Yaw in degrees from the Player actor's 3x3 rotation (9 floats, rows in memory order), or None when it isn't
    a turn about y. Row 0 is (cos a, 0, sin a); TotK Recomp's control.c measures it the same way (checked there
    against Link walking in each direction), so both sources agree."""
    if not rot:
        return None
    c, s = rot[0], rot[2]
    n = c * c + s * s
    if not (math.isfinite(n) and 0.5 < n < 1.5):
        return None
    return round(math.degrees(math.atan2(s, c)), 1)


# A shrine is its own scene ('Dungeon062'), named as the shrines in map_data.json (internal_name). Inside one,
# Link's position is in the shrine's own coordinates, which mean nothing on the world map.
SHRINE_SCENE = re.compile(r'Dungeon\d{3}')


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    z: float
    yaw: float = None
    scene: str = None      # the loaded scene, when the source knows it ('Dungeon062' in that shrine)
    source: str = ''
    at: float = field(default_factory=time.time)

    @property
    def heading(self):
        return yaw_to_heading(self.yaw)

    @property
    def shrine(self):
        """The shrine Link is in (its internal name, as in map_data.json), or None."""
        return self.scene if self.scene and SHRINE_SCENE.fullmatch(self.scene) else None

    @property
    def on_map(self):
        """x, y, z are world coordinates (False inside a shrine)."""
        return self.shrine is None

    def to_json(self):
        return {'x': round(self.x, 2), 'y': round(self.y, 2), 'z': round(self.z, 2),
                'yaw': self.yaw, 'heading': self.heading, 'scene': self.scene, 'shrine': self.shrine,
                'on_map': self.on_map, 'source': self.source, 'at': round(self.at, 3)}


@dataclass(frozen=True)
class GameState:
    in_world: bool                # a save is loaded and the game runs
    game: str = None              # game version, e.g. '1.4.3'
    recomp: str = None            # TotK Recomp version
    world_s: float = None         # game time since the save loaded
    source: str = ''
    at: float = field(default_factory=time.time)

    def to_json(self):
        return {'in_world': self.in_world, 'game': self.game, 'recomp': self.recomp, 'world_s': self.world_s,
                'source': self.source, 'at': round(self.at, 3)}


@dataclass(frozen=True)
class ProgressSnapshot:
    """The flags a progress.sav holds, from a save file or from the running game.

    flags: {u32 hash: u32 value} (floats as their bits, signed ints as two's complement), as savefile.read_table
    guids: the GUIDs whose flag is set (opened chests, read signs, ...), as savefile.read_guids
    save_pos: where Link was at the save (a save file only; the running game reports a live Pose instead)
    slot: the save slot it came from, or None for the running game
    inventory: (frozenset of armor actors carried, {material actor: count}) as savefile.read_inventory, or None
        when the source can't read the inventory (the running game, for now)
    """
    flags: dict
    guids: frozenset
    source: str
    slot: int = None
    version_id: int = None
    save_pos: Pose = None
    inventory: tuple = None
    mtime: float = None           # when the save file was written (save files only)
    at: float = field(default_factory=time.time)

    @property
    def playtime(self):
        return self.flags.get(PLAYTIME_HASH, 0)


@dataclass(frozen=True)
class SourceStatus:
    name: str
    available: bool
    capabilities: Capability = Capability.NONE
    error: str = None
    detail: dict = field(default_factory=dict)

    def to_json(self):
        caps = [c.name.lower() for c in Capability if c in self.capabilities]
        return {'name': self.name, 'available': self.available, 'capabilities': caps, 'error': self.error,
                **self.detail}


class GameSource:
    """One way of reading the game. Subclasses set `name` and `capabilities` and override what they offer;
    anything else raises SourceUnavailable. Every method may be called from several threads."""
    name = 'source'
    capabilities = Capability.NONE
    experimental = False   # reads the game in a way that is only a stopgap (e.g. its memory)

    def status(self):
        return SourceStatus(self.name, True, self.capabilities)

    def pose(self):
        raise SourceUnavailable(f'{self.name} has no live position')

    def state(self):
        raise SourceUnavailable(f'{self.name} has no game state')

    def progress(self, slot=None):
        raise SourceUnavailable(f'{self.name} has no progress')

    def close(self):
        pass
