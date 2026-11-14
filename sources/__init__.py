"""Where the app gets game data from, behind one interface (model.GameSource).

- savefile_source: the save files. Always there, as of the last save.
- control_source: the running game, through TotK Recomp's control socket (the game's own read-only interface for
  companion apps) and, for what that doesn't answer yet, totk-live-bridge, a stopgap that reads the game's memory
  and answers in the same protocol.

Everything else in the app works with the model types (Pose, GameState, ProgressSnapshot) and never with a
source's own way of reading.
"""
from .model import (SHRINE_SCENE, Capability, GameSource, GameState, Pose, ProgressSnapshot, SourceStatus,
                    SourceUnavailable, yaw_from_rotation, yaw_to_heading)
from .savefile_source import SaveFileSource
from .control_source import ControlSource, LiveSource, start_bridge

__all__ = ['SHRINE_SCENE', 'Capability', 'GameSource', 'GameState', 'Pose', 'ProgressSnapshot', 'SourceStatus', 'SourceUnavailable',
           'yaw_from_rotation', 'yaw_to_heading', 'SaveFileSource', 'ControlSource', 'LiveSource', 'start_bridge']
