"""Experimental source: the running game's memory (sources.memory_reader). Live progress and Link's position.

A stopgap until TotK Recomp's control socket answers everything the app needs; see memory_reader for why.
"""
from .memory_reader import GAME_VERSION, LiveError, LiveReader, SUPPORTED
from .model import Capability, GameSource, GameState, Pose, ProgressSnapshot, SourceStatus, SourceUnavailable, \
    yaw_from_rotation


class MemorySource(GameSource):
    name = 'memory'
    capabilities = Capability.POSE | Capability.STATE | Capability.PROGRESS | Capability.INVENTORY
    experimental = True

    def __init__(self, reader=None):
        self.reader = reader or LiveReader()

    def status(self):
        st = self.reader.status()
        return SourceStatus(self.name, st['available'], self.capabilities, st['error'],
                            {'pid': st['pid'], 'experimental': True, 'supported': SUPPORTED})

    def pose(self):
        try:
            (x, y, z), rot = self.reader.player()
            scene = self.reader.scene()
        except LiveError as e:
            raise SourceUnavailable(str(e)) from e
        pose = Pose(x, y, z, yaw_from_rotation(rot), scene=scene, source=self.name)
        # the title screen keeps the Player at the origin (as control.c checks); a shrine's own coordinates may
        # well be near 0, so only on the map
        if pose.on_map and abs(x) + abs(z) < 1:
            raise SourceUnavailable('No save loaded')
        return pose

    def state(self):
        """Whether a save is loaded. The reader only reads a build whose layout it checked, so the version is known."""
        try:
            self.pose()
            in_world = True
        except SourceUnavailable:
            in_world = False
        return GameState(in_world, game=GAME_VERSION if self.reader.error is None else None, source=self.name)

    def progress(self, slot=None):
        if slot not in (None, 'live'):
            raise SourceUnavailable('the running game has no save slots')
        try:
            flags, guids = self.reader.snapshot()
        except LiveError as e:
            raise SourceUnavailable(str(e)) from e
        try:   # the progress doesn't need it: without it the app shows the outfits of the latest save
            carried, mats = self.reader.inventory()
            inventory = (frozenset(carried), mats)
        except LiveError:
            inventory = None
        return ProgressSnapshot(flags, frozenset(guids), source=self.name, inventory=inventory)
