# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Trailing-edge debounce + single-flight for ``crux super refresh``.

A super PR fans across N repos, so a coordinated update (push all the member
branches, or one CI job that pushes several) fires the pre-push hook N times,
and every one of them detaches ``crux super refresh <n> --delay 15`` for the
*same* super PR. Without coordination that is N concurrent LLM passes all
writing the one brief — wasteful, and a last-writer-wins race on the file.

What we want instead, per super-PR number:

* **one at a time** — never two refreshes of the same super PR at once;
* **skip the middle** — a burst collapses to a single run;
* **the last always runs** — whichever push is newest when the dust settles
  gets analyzed, so the published brief reflects the final state.

That is a *trailing-edge* debounce (fire after the burst, on the latest
input) plus a run lock. It is machine-local: the files live under
``~/.cache/crux`` (the same root as the run cache, and like it redirectable
via ``$HOME`` so tests are hermetic). Cross-machine brief-write ordering is a
different concern handled by the brief ``rev`` counter, not here.

How it works, for one ``super refresh`` invocation:

1. **Claim** — stamp "a refresh of <n> was requested now" (a monotonic-ish
   ``time_ns`` token) into ``super-<n>.stamp``, keeping the max. The stamp is
   returned to the caller as its ticket.
2. **Wait** — sleep the debounce delay. Newer pushes arriving in this window
   write larger stamps.
3. **Yield if superseded** — if the stamp on disk is now larger than our
   ticket, a newer push owns the trailing run; we skip. Only the newest
   ticket survives, so exactly one process proceeds.
4. **Single-flight** — take a blocking lock on ``super-<n>.run.lock`` so an
   already-running refresh (of an earlier burst) finishes first, then
   re-check supersession once more (a push may have landed while we waited on
   the lock). The survivor runs the refresh under the lock.

Every failure degrades to *doing the refresh* rather than skipping it: a
broken stamp file, an unreadable lock, a clock that went backwards — none of
those may cause the trailing run to be silently dropped, because a missed
refresh means the brief is stale and nobody is told. The only thing the lock
prevents is *concurrency*; when in doubt we run.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Iterator

# How long to wait for an in-progress refresh of the same super PR to finish
# before giving up the single-flight lock. Generous on purpose: a real refresh
# is an LLM pass measured in minutes, and timing out would drop the trailing
# run. If this is ever hit, the log says so and we run anyway (a concurrent
# write is a smaller sin than a silently stale brief).
_RUN_LOCK_TIMEOUT_S = 600.0

# Poll interval while blocking on the run lock. flock(2) has no timed variant,
# so we spin non-blocking acquires. Coarse — this path is idle waiting.
_RUN_LOCK_POLL_S = 0.5


def _dir() -> Path:
    # expanduser (not a literal) so $HOME redirection makes tests hermetic —
    # same root and rationale as crux.cache.
    d = Path(os.path.expanduser("~/.cache/crux")) / "super-debounce"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp_path(number: int) -> Path:
    return _dir() / f"super-{number}.stamp"


def _run_lock_path(number: int) -> Path:
    return _dir() / f"super-{number}.run.lock"


def _read_stamp(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        # Absent or garbage reads as "no prior request". A corrupt stamp must
        # never look NEWER than a real one — that would wrongly supersede a
        # genuine trailing run — so it reads as the oldest possible value.
        return 0


def claim(number: int) -> int:
    """Record that a refresh of super PR *number* was requested, and return
    this request's ticket. The ticket is compared against the stored stamp
    after the delay to decide who owns the trailing run.

    The read-modify-write is serialized by a short lock so two near-instant
    claims cannot both read the old value and clobber each other; the file
    only ever moves forward (``max``), so a late claim with a smaller clock
    reading still cannot lower it.
    """
    ticket = time.time_ns()
    try:
        path = _stamp_path(number)
        lock_path = path.with_suffix(".stamp.lock")
        with _flock(lock_path, blocking=True):
            current = _read_stamp(path)
            newest = max(current, ticket)
            path.write_text(str(newest), encoding="utf-8")
            # Our effective ticket is what we actually committed as the max:
            # if the clock went backwards and `current` was larger, adopt it
            # so we don't immediately think we were superseded by ourselves.
            return newest
    except OSError:
        # Could not take the stamp lock, or even create the state dir (a
        # read-only cache). Best-effort write, then fall back to our own
        # ticket. Worst case we fail to coalesce and run an extra refresh; we
        # never drop the trailing one, and we never crash the refresh.
        with contextlib.suppress(OSError):
            _stamp_path(number).write_text(str(ticket), encoding="utf-8")
        return ticket


def superseded(number: int, ticket: int) -> bool:
    """True if a newer request has since claimed super PR *number* — meaning
    some later push owns the trailing run and this one should skip.

    Equal stamps are NOT superseded: the holder of the newest ticket sees its
    own value and proceeds. ANY failure returns False (not superseded) — an
    unreadable stamp, or a state dir that cannot even be created, must never
    silently cancel the trailing refresh. The cost of a false "not superseded"
    is at most an extra run; the cost of a wrong "superseded" is a dropped
    refresh and a stale brief.
    """
    try:
        return _read_stamp(_stamp_path(number)) > ticket
    except OSError:
        return False


@contextlib.contextmanager
def _flock(path: Path, blocking: bool) -> Iterator[bool]:
    """Hold an exclusive ``flock`` on *path* for the ``with`` body.

    Yields True if the lock was acquired, False if ``blocking`` is False and
    it was already held. The fd is kept open for the body's duration (closing
    it is what releases the lock) and always closed on exit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except OSError as exc:
            if not blocking and exc.errno in (errno.EAGAIN, errno.EACCES):
                yield False
                return
            raise
        yield True
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def single_flight(number: int, log=None) -> Iterator[bool]:
    """Serialize refreshes of super PR *number*. Blocks until any in-progress
    refresh of the same super PR finishes (up to ``_RUN_LOCK_TIMEOUT_S``),
    then yields True to run the body under the lock.

    Yields True even on timeout — a stuck predecessor must not cost us the
    trailing run; we log and accept a rare concurrent write rather than drop
    the refresh. Yields False only if the lock file itself is unusable, and
    even then the caller runs (see the call site): the lock only ever prevents
    concurrency, never the refresh.
    """
    deadline = time.monotonic() + _RUN_LOCK_TIMEOUT_S
    try:
        lock_path = _run_lock_path(number)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # Cannot open the run lock, or even create the state dir. Run without
        # single-flight rather than skip: the lock only prevents concurrency,
        # never the refresh itself.
        if log:
            log.warning("super-debounce: cannot open run lock for super #%d "
                        "(%s); running without single-flight", number, exc)
        yield True
        return
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break  # got it
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    if log:
                        log.warning(
                            "super-debounce: waited %.0fs for an in-progress "
                            "refresh of super #%d; running anyway to avoid a "
                            "stale brief", _RUN_LOCK_TIMEOUT_S, number)
                    break  # run anyway rather than drop the trailing refresh
                time.sleep(_RUN_LOCK_POLL_S)
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
