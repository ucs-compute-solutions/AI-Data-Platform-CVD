"""Deterministic source-level model of the p3 NvStreamer replay barrier.

This does not run NvStreamer, GStreamer, RTSP, or OpenShift. The legacy model
transcribes the EOS/replay-list behavior shown as removed lines in
../0003-count-eos-by-active-session.patch. That patch is review-only and is
not part of the tested p3 image. These tests expose why p3 needs another
source change or upstream confirmation before production promotion.
"""

from dataclasses import dataclass
import unittest


@dataclass(frozen=True)
class Reader:
    session_id: str
    filename: str


READERS = (
    Reader("cam01-reader-a", "Camera_01.mp4"),
    Reader("cam01-reader-b", "Camera_01.mp4"),
    Reader("cam00-reader", "Camera.mp4"),
    Reader("cam02-reader", "Camera_02.mp4"),
    Reader("cam03-reader", "Camera_03.mp4"),
)


class P3FilenameCallbackCount:
    """Model the unmodified p3 m_replayList and m_mediaSourceList operations."""

    def __init__(self, readers):
        self.known = {reader.session_id: reader for reader in readers}
        self.active = dict(self.known)
        self.replay_filenames = []
        self.replay_count = 0
        self.replay_snapshots = []

    def add(self, reader):
        self.known[reader.session_id] = reader
        self.active[reader.session_id] = reader

    def remove(self, session_id):
        self.active.pop(session_id)
        if not self.active:
            self.replay_filenames.clear()
        elif self.replay_filenames and len(self.replay_filenames) >= len(self.active):
            self._replay()

    def eos(self, session_id):
        # The bus callback receives the demux object's filename. The demux
        # may still signal after its RTSP source was removed from the list.
        self.replay_filenames.append(self.known[session_id].filename)
        if self.active and len(self.replay_filenames) >= len(self.active):
            self._replay()

    def _replay(self):
        self.replay_snapshots.append((set(self.active), tuple(self.replay_filenames)))
        self.replay_count += 1
        self.replay_filenames.clear()


class ActiveSessionSet:
    """Proposed 0003 behavior: one EOS per active session, without an epoch."""

    def __init__(self, readers):
        self.active = {reader.session_id: reader for reader in readers}
        self.arrived = set()
        self.replay_count = 0

    def add(self, reader):
        self.active[reader.session_id] = reader

    def remove(self, session_id):
        self.active.pop(session_id)
        self.arrived.discard(session_id)
        if not self.active:
            self.arrived.clear()
        elif self.arrived and len(self.arrived) >= len(self.active):
            self._replay()

    def eos(self, session_id):
        if session_id not in self.active:
            return
        self.arrived.add(session_id)
        if len(self.arrived) >= len(self.active):
            self._replay()

    def _replay(self):
        self.replay_count += 1
        self.arrived.clear()


class EosAccountingRegression(unittest.TestCase):
    def setUp(self):
        self.p3 = P3FilenameCallbackCount(READERS)
        self.expected = ActiveSessionSet(READERS)

    def emit(self, *session_ids):
        for session_id in session_ids:
            self.p3.eos(session_id)
            self.expected.eos(session_id)

    def test_normal_one_eos_per_active_reader_replays_once(self):
        self.emit(*(reader.session_id for reader in READERS))
        self.assertEqual(self.p3.replay_count, 1)
        self.assertEqual(self.expected.replay_count, 1)

    def test_one_duplicate_eos_can_replay_before_last_reader_finishes(self):
        self.emit(READERS[0].session_id, READERS[0].session_id,
                  READERS[1].session_id, READERS[2].session_id,
                  READERS[3].session_id)
        self.assertEqual(self.p3.replay_count, 1)
        self.assertEqual(self.expected.replay_count, 0)
        self.assertEqual(set(self.expected.active) - self.expected.arrived,
                         {READERS[4].session_id})
        self.assertEqual(self.p3.replay_snapshots[0][1],
                         ("Camera_01.mp4", "Camera_01.mp4",
                          "Camera_01.mp4", "Camera.mp4", "Camera_02.mp4"))

    def test_stale_disconnected_reader_can_replay_before_replacement_finishes(self):
        old_reader = READERS[0]
        replacement = Reader("cam01-reader-c", old_reader.filename)
        for barrier in (self.p3, self.expected):
            barrier.remove(old_reader.session_id)
            barrier.add(replacement)

        # A queued EOS from the old demux arrives after removal. All other
        # readers reach EOS, but the replacement has not yet done so.
        self.emit(old_reader.session_id,
                  *(reader.session_id for reader in READERS[1:]))
        self.assertEqual(self.p3.replay_count, 1)
        self.assertEqual(self.expected.replay_count, 0)
        self.assertEqual(set(self.expected.active) - self.expected.arrived,
                         {replacement.session_id})
        self.assertEqual(len(self.p3.replay_snapshots[0][1]), len(READERS))

    def test_removing_unfinished_reader_unblocks_valid_survivors(self):
        self.emit(*(reader.session_id for reader in READERS[1:]))
        self.assertEqual(self.p3.replay_count, 0)
        self.assertEqual(self.expected.replay_count, 0)
        for barrier in (self.p3, self.expected):
            barrier.remove(READERS[0].session_id)
        self.assertEqual(self.p3.replay_count, 1)
        self.assertEqual(self.expected.replay_count, 1)

    def test_old_loop_eos_from_active_reader_breaks_session_only_patch(self):
        # A complete first loop clears the set. A delayed callback from that
        # loop's still-active reader A can then count toward the second loop.
        self.emit(*(reader.session_id for reader in READERS))
        self.assertEqual(self.expected.replay_count, 1)
        self.expected.eos(READERS[0].session_id)  # old-loop A
        for reader in READERS[1:]:
            self.expected.eos(reader.session_id)  # current-loop B-E
        self.assertEqual(self.expected.replay_count, 2)
        # A has not reached EOS for the current loop. This proves that 0003
        # needs a playback-generation check in addition to session identity.


if __name__ == "__main__":
    unittest.main(verbosity=2)
