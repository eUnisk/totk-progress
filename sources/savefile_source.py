"""Source: the save files (progress.sav in profile/save/<user>/slot_0N). Always works, but only as of the last save."""
import struct
import time
from pathlib import Path

import savefile

from .model import Capability, GameSource, Pose, ProgressSnapshot, SourceStatus, SourceUnavailable


class SaveFileSource(GameSource):
    name = 'save'
    capabilities = Capability.PROGRESS | Capability.INVENTORY

    def __init__(self, save_dir=None):
        self.save_dir = Path(save_dir) if save_dir else None

    def path(self, slot):
        if not self.save_dir:
            raise SourceUnavailable('no save folder set up')
        return self.save_dir / f'slot_{int(slot):02d}' / 'progress.sav'

    def status(self):
        ok = bool(self.save_dir and self.save_dir.is_dir())
        return SourceStatus(self.name, ok, self.capabilities, None if ok else 'no save folder')

    def progress(self, slot=None):
        """The flags of one slot. The game may be writing the file: a torn read is retried a few times."""
        if slot is None:
            raise SourceUnavailable('which save slot?')
        f = self.path(slot)
        last = None
        for _ in range(5):
            try:
                mtime = f.stat().st_mtime
                buf = f.read_bytes()
                table, guid_offset = savefile.read_table(buf)
                guids = savefile.read_guids(buf, guid_offset)
                pos = savefile.player_position(buf, table)
                carried, mats = savefile.read_inventory(buf, table)
                break
            except (savefile.SaveError, struct.error, OSError) as e:
                last = e
                time.sleep(0.3)
        else:
            raise savefile.SaveError(f'could not read slot {slot}: {last}')
        return ProgressSnapshot(
            table, frozenset(guids), source=self.name, slot=int(slot), mtime=mtime,
            version_id=struct.unpack_from('<I', buf, 4)[0], inventory=(frozenset(carried), mats),
            save_pos=Pose(pos['x'], pos['y'], pos['z'], source=self.name, at=mtime) if pos else None)
