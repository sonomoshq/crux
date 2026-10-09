# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""The trailing-edge debounce + single-flight behind ``crux super refresh``.

The behaviours pinned here are the contract the pre-push hook relies on when a
super PR fanned over several repos is pushed all at once: run one at a time,
skip the middle, and always run the last.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import crux.superdebounce as sd


class TestSuperDebounce(unittest.TestCase):
    def setUp(self) -> None:
        # Redirect the state dir into a throwaway HOME — superdebounce keys off
        # ~/.cache/crux via expanduser, so a fresh $HOME is a clean, isolated
        # store that never touches the real one.
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_the_newest_claim_wins_and_the_earlier_ones_are_superseded(self) -> None:
        # A burst: three pushes claim the same super PR in order. Only the last
        # ticket survives the supersession check; the earlier two stand down.
        first = sd.claim(1)
        second = sd.claim(1)
        third = sd.claim(1)
        self.assertTrue(sd.superseded(1, first), "an earlier push must yield")
        self.assertTrue(sd.superseded(1, second), "the middle push too")
        self.assertFalse(sd.superseded(1, third), "the last push always runs")

    def test_supersession_is_per_super_pr(self) -> None:
        # A claim on #2 must not supersede an outstanding claim on #1.
        one = sd.claim(1)
        sd.claim(2)
        self.assertFalse(sd.superseded(1, one),
                         "another super PR's push is unrelated")

    def test_a_single_claim_runs(self) -> None:
        ticket = sd.claim(7)
        self.assertFalse(sd.superseded(7, ticket))

    def test_a_corrupt_stamp_never_cancels_the_trailing_run(self) -> None:
        # A garbage stamp reads as "no prior request" (oldest possible), so it
        # can never look newer than a real ticket and wrongly supersede it.
        ticket = sd.claim(1)
        sd._stamp_path(1).write_text("not-a-number", encoding="utf-8")
        self.assertFalse(sd.superseded(1, ticket))

    def test_a_broken_state_dir_never_crashes_or_cancels(self) -> None:
        # The module's promise: every failure degrades to DOING the refresh.
        # If even the state dir cannot be reached, claim still returns a
        # ticket, superseded says no, and single_flight still runs the body —
        # none of them raise.
        with mock.patch.object(sd, "_dir", side_effect=OSError("read-only")):
            ticket = sd.claim(1)          # must not raise
            self.assertIsInstance(ticket, int)
            self.assertFalse(sd.superseded(1, ticket))
            ran = False
            with sd.single_flight(1):
                ran = True
            self.assertTrue(ran, "a broken lock dir must not skip the run")

    def test_single_flight_serializes_runs_of_the_same_super_pr(self) -> None:
        # Two threads both hold single_flight(1); the second must not enter the
        # body until the first leaves. A shared counter catches any overlap.
        order: list[str] = []
        inside = threading.Event()
        release = threading.Event()

        def first() -> None:
            with sd.single_flight(1):
                order.append("first-enter")
                inside.set()
                release.wait(2.0)
                order.append("first-exit")

        def second() -> None:
            inside.wait(2.0)          # let `first` take the lock
            with sd.single_flight(1):
                order.append("second-enter")

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        time.sleep(0.2)              # give `second` time to block on the lock
        self.assertEqual(order, ["first-enter"],
                         "second must be blocked while first holds the lock")
        release.set()
        t1.join(2.0)
        t2.join(2.0)
        self.assertEqual(order, ["first-enter", "first-exit", "second-enter"],
                         "second only runs after first releases")

    def test_single_flight_of_different_super_prs_do_not_block(self) -> None:
        # Independent super PRs must run concurrently — the lock is per number.
        with sd.single_flight(1):
            entered_two = False
            with sd.single_flight(2):
                entered_two = True
            self.assertTrue(entered_two,
                            "a different super PR's lock is independent")


if __name__ == "__main__":
    unittest.main()
