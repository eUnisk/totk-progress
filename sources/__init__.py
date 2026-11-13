"""Where the app gets game data from, behind one interface (model.GameSource).

- savefile_source: the save files. Always there, as of the last save.
- memory_source: the running game's memory. Experimental and a stopgap (see memory_reader).
- (planned) TotK Recomp's control socket: the game's own read-only interface for companion apps.

Everything else in the app works with the model types (Pose, GameState, ProgressSnapshot) and never with a
source's own way of reading.
"""
from .model import (SHRINE_SCENE, Capability, GameSource, GameState, Pose, ProgressSnapshot, SourceStatus,
                    SourceUnavailable, yaw_from_rotation, yaw_to_heading)
from .savefile_source import SaveFileSource
from .memory_source import MemorySource

__all__ = ['SHRINE_SCENE', 'Capability', 'GameSource', 'GameState', 'Pose', 'ProgressSnapshot', 'SourceStatus', 'SourceUnavailable',
           'yaw_from_rotation', 'yaw_to_heading', 'SaveFileSource', 'MemorySource']
