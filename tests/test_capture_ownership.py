"""Regression tests for shared-capture ownership races.

Two transports (cloud, LAN) share ONE parec capture feeding ONE PCM queue. Two live
consumers halve each side's frame rate (the original 25fps/331ms-jitter bug). Two
races made the earlier "is the other one streaming?" predicates insufficient:

  1. Both transports answer "not streaming" during the seconds-wide window between
     deciding to start and installing their peer, so both attach a track.
  2. A stop offloaded to an executor can land AFTER a newer start and terminate the
     fresh parec under a live peer -- the phone then hears pure silence with no
     error to trigger a retry.

Run: venv/bin/python tests/test_capture_ownership.py
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.audio_relay import AudioRelay


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def _fake_started(relay):
    """Put the relay into a 'running' state without spawning a real parec."""
    relay._parec_proc = FakeProc()
    relay._running = True
    relay._generation += 1
    return relay._parec_proc


def test_ownership_is_exclusive():
    relay = AudioRelay()
    assert relay.owner is None

    assert relay.claim("cloud") is None, "first claim has no previous owner"
    assert relay.owner == "cloud"

    # The LAN path claiming must report the cloud as previous owner so it knows to
    # evict it. Without this the two transports both keep a track on the queue.
    assert relay.claim("lan") == "cloud", (
        "REGRESSION: claim did not report the displaced owner, so the previous "
        "transport is never closed and both split the capture"
    )
    assert relay.owner == "lan"

    # A non-owner releasing must be a no-op: otherwise the cloud's teardown would
    # stop the capture out from under a live LAN session (silent audio).
    assert relay.release("cloud") is False, (
        "REGRESSION: a stale transport released ownership it no longer holds"
    )
    assert relay.owner == "lan", "ownership must survive a stale release"

    assert relay.release("lan") is True
    assert relay.owner is None
    print("PASS: ownership token is exclusive and evicts correctly")


def test_stale_stop_is_ignored():
    relay = AudioRelay()
    proc1 = _fake_started(relay)
    gen1 = relay.generation

    # Session 1 ends; its stop is captured but not yet delivered (executor hop).
    relay.stop(generation=gen1)
    assert proc1.terminated, "the owning generation's stop must terminate its parec"

    # A new session starts before that stale stop would have landed.
    proc2 = _fake_started(relay)
    assert relay.generation != gen1, "a new capture must bump the generation"

    # The late stop from session 1 must NOT kill session 2's capture.
    relay.stop(generation=gen1)
    assert not proc2.terminated, (
        "REGRESSION: a stale stop terminated the current capture -- the live peer "
        "would stream pure silence with no error and no retry"
    )
    assert relay._running, "current capture must still be running"

    # An unversioned stop still works (explicit shutdown).
    relay.stop()
    assert proc2.terminated, "unversioned stop must always terminate"
    print("PASS: stale generation stop cannot kill a newer capture")


def test_concurrent_claims_have_one_winner():
    relay = AudioRelay()
    results = []
    barrier = threading.Barrier(2)

    def claim(name):
        barrier.wait()
        results.append((name, relay.claim(name)))

    t1 = threading.Thread(target=claim, args=("cloud",))
    t2 = threading.Thread(target=claim, args=("lan",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # Exactly one claimer must see "no previous owner"; the other must see the first,
    # so it knows to evict it. Both seeing None would mean two live tracks.
    firsts = [r for _, r in results if r is None]
    assert len(firsts) == 1, (
        f"REGRESSION: {len(firsts)} claimers saw an empty capture; concurrent starts "
        f"would each attach a track and halve the frame rate (results={results})"
    )
    print("PASS: concurrent claims produce exactly one winner")


def test_failed_start_releases_the_claim():
    """A transport that claims then fails to start must not pin ownership.

    Otherwise the token stays held by a transport that never streamed, every later
    release() by the real owner returns False, and the capture is never stopped again.
    """
    relay = AudioRelay()
    relay.claim("lan")
    # ... start fails (no monitor device); production code calls release on that path.
    relay.release("lan")
    assert relay.owner is None, (
        "REGRESSION: a failed start left ownership pinned -- no later stop can ever "
        "release the capture, so parec runs for the process lifetime"
    )

    # The other transport can now claim and, crucially, later release it.
    relay.claim("cloud")
    assert relay.release("cloud") is True
    print("PASS: a failed start releases its claim")


def test_peer_death_releases_ownership():
    """When the last peer on a transport closes, ownership must be released.

    A never-answered peer (or ICE failure) otherwise leaves the token held with zero
    consumers: parec keeps running and burning CPU with nobody reading the queue.
    """
    relay = AudioRelay()
    relay.claim("lan")
    _fake_started(relay)

    # Peer closed -> production _on_peer_closed releases and stops.
    released = relay.release("lan")
    assert released, (
        "REGRESSION: peer death did not release ownership -- capture would keep "
        "running with no consumer"
    )
    relay.stop()
    assert relay.owner is None
    assert not relay._running, "capture must stop once its last consumer is gone"
    print("PASS: peer death releases ownership and the capture stops")


def test_owner_gates_late_initiate():
    """A late cloud initiate must be refused while the LAN path owns the capture.

    This is the exact shape of the original 25fps bug: the cloud webrtc_initiate is
    delayed by a round trip, arrives after the phone switched to LAN, and attaches a
    SECOND track to the shared queue.
    """
    relay = AudioRelay()
    relay.claim("lan")

    # Mirrors the production guard in OrchestratorClient._handle_webrtc_initiate.
    may_attach = relay.owner in (None, "cloud")
    assert not may_attach, (
        "REGRESSION: a late cloud initiate would attach a second track while LAN owns "
        "the capture -- both consumers split the 50fps queue and the phone gets 25fps"
    )

    # Once LAN releases, the cloud is allowed again.
    relay.release("lan")
    assert relay.owner in (None, "cloud"), "cloud must be admitted once LAN releases"
    print("PASS: ownership gates a late cloud initiate")


def test_start_while_running_bumps_generation():
    """Reusing a running capture must still bump the generation.

    A stop captured against the OLD generation may be in flight (peer death schedules
    it on a thread). If start() returns early without bumping, that stop matches and
    terminates parec under the session that just claimed it -- the phone connects and
    hears pure silence, with no error to trigger a retry.
    """
    relay = AudioRelay()
    proc = _fake_started(relay)
    gen_before = relay.generation

    # A second transport claims and starts while the capture is already running.
    assert relay._start_locked() is True, "start on a running capture should succeed"
    assert relay.generation != gen_before, (
        "REGRESSION: start() reused a running capture without bumping the generation; "
        "an in-flight stale stop will now kill the fresh session's capture"
    )

    # The stale stop must be refused.
    relay.stop(generation=gen_before)
    assert not proc.terminated, (
        "REGRESSION: stale stop terminated the capture the new owner just took over"
    )
    print("PASS: start on a running capture bumps the generation")


def test_release_and_stop_is_atomic():
    """release+stop must be one step under the lock.

    As two steps, the other transport can claim and start in between, and the stop
    then kills that fresh capture under a live peer.
    """
    relay = AudioRelay()
    relay.claim("cloud")
    _fake_started(relay)

    # The owner tears down: releases AND stops atomically.
    assert relay.release_and_stop("cloud") is True
    assert relay.owner is None
    assert not relay._running

    # A non-owner must neither release nor stop.
    relay.claim("lan")
    proc2 = _fake_started(relay)
    assert relay.release_and_stop("cloud") is False, (
        "REGRESSION: a non-owner released the capture"
    )
    assert not proc2.terminated, (
        "REGRESSION: a non-owner's teardown stopped the live owner's capture -- "
        "the phone would hear silence with no error"
    )
    assert relay.owner == "lan", "the real owner must keep the capture"
    print("PASS: release_and_stop is atomic and owner-scoped")


def test_delayed_release_and_stop_respects_generation():
    """A delayed teardown must not kill a NEWER session that reused the same owner name.

    Peer-death teardowns run on a thread/executor, so by the time they execute the same
    transport name can legitimately belong to a fresh session. Owner identity alone is
    not enough -- without the generation check the late call kills the new session's
    capture and the phone hears pure silence with no error and no retry.
    """
    relay = AudioRelay()
    relay.claim("cloud")
    _fake_started(relay)
    stale_gen = relay.generation

    # Session ends; its teardown is scheduled but has not run yet.
    # Meanwhile the SAME transport starts a new session.
    relay.claim("cloud")
    proc2 = _fake_started(relay)
    assert relay.generation != stale_gen

    # The delayed teardown from the old session now runs.
    killed = relay.release_and_stop("cloud", generation=stale_gen)
    assert killed is False, (
        "REGRESSION: a delayed teardown tore down a newer session that reused the same "
        "owner name -- generation is being ignored"
    )
    assert not proc2.terminated, "the new session's capture must survive"
    assert relay.owner == "cloud", "the new session must keep ownership"

    # The current generation's teardown still works.
    assert relay.release_and_stop("cloud", generation=relay.generation) is True
    assert proc2.terminated
    print("PASS: delayed release_and_stop respects the generation")


def test_predicate_runs_under_the_lock():
    """release_and_stop_if must not act when the predicate says no."""
    relay = AudioRelay()
    relay.claim("cloud")
    proc = _fake_started(relay)

    # A peer appeared -> predicate False -> capture must be left alone.
    assert relay.release_and_stop_if("cloud", lambda: False) is False
    assert not proc.terminated, (
        "REGRESSION: the watchdog stopped a capture whose session had just started"
    )
    assert relay.owner == "cloud"

    # No peer -> predicate True -> released.
    assert relay.release_and_stop_if("cloud", lambda: True) is True
    assert proc.terminated and relay.owner is None
    print("PASS: release_and_stop_if honours its predicate under the lock")


def test_claim_invalidates_an_inflight_stop():
    """A stop scheduled before a re-claim must not kill the capture that follows it.

    The phone can send stop_audio_relay and start_audio_relay back to back. The stop is
    offloaded to a thread; the reader thread then re-claims for the SAME transport. If
    claim() does not bump the generation, the in-flight stop still matches (owner is
    right, and start() has not run yet to bump it), so it tears down ownership and kills
    the parec the new session is about to use -- leaving owner=None, which then makes
    every later webrtc_initiate refused and the capture unstoppable. Pure silence with
    no error and no retry.
    """
    relay = AudioRelay()
    relay.claim("cloud")
    _fake_started(relay)

    # Teardown captured for the CURRENT session, delivery delayed.
    stale_gen = relay.generation

    # Same transport immediately re-claims (the stop/start pair).
    relay.claim("cloud")

    # The delayed stop now lands.
    killed = relay.release_and_stop("cloud", generation=stale_gen)
    assert killed is False, (
        "REGRESSION: a stop scheduled before the re-claim tore down the new session -- "
        "claim() must invalidate in-flight teardowns"
    )
    assert relay.owner == "cloud", (
        "REGRESSION: ownership was cleared to None; every later initiate would be "
        "refused and the capture could never be released"
    )
    print("PASS: claim invalidates an in-flight stop")


def test_stop_waiting_on_the_lock_cannot_kill_a_new_capture():
    """A teardown blocked on the capture lock must re-verify before stopping.

    Ownership and the capture use separate locks (so claim() is never blocked behind
    parec's terminate+join, which would stall the event loop). That leaves a gap: a
    teardown can pass the ownership check, drop the ownership lock, then wait for the
    capture lock while ANOTHER transport claims and starts. Without a re-verify it then
    terminates the parec that new session just spawned -- the phone connects and hears
    pure silence, with no error and no retry.
    """
    relay = AudioRelay()
    relay.claim("lan")
    _fake_started(relay)
    gen = relay.generation

    holder_in = threading.Event()
    release_holder = threading.Event()

    def hold_capture_lock():
        with relay._lock:
            holder_in.set()
            release_holder.wait(5)

    holder = threading.Thread(target=hold_capture_lock, daemon=True)
    holder.start()
    assert holder_in.wait(2), "helper failed to take the capture lock"

    # The LAN teardown starts: it passes the ownership check, then blocks on _lock.
    result = {}

    def lan_teardown():
        result["stopped"] = relay.release_and_stop("lan", generation=gen)

    teardown = threading.Thread(target=lan_teardown, daemon=True)
    teardown.start()
    time.sleep(0.2)  # let it get past the ownership check and block on _lock

    # Meanwhile the cloud claims and starts a fresh capture.
    relay.claim("cloud")
    proc_new = _fake_started(relay)

    # Now let the teardown proceed.
    release_holder.set()
    teardown.join(5)

    assert result.get("stopped") is False, (
        "REGRESSION: a teardown that waited on the capture lock stopped the capture "
        "anyway -- it must re-verify the generation after acquiring the lock"
    )
    assert not proc_new.terminated, (
        "REGRESSION: the teardown terminated the capture the new session just started; "
        "the phone would hear pure silence with no error and no retry"
    )
    assert relay.owner == "cloud", "the new owner must keep the capture"
    print("PASS: a teardown blocked on the lock cannot kill a newer capture")



def test_stale_release_cannot_strip_a_newer_session():
    """A superseded flow must not release the token a NEWER session now holds.

    Owner names are reused, so a flow that resumes after being superseded would match
    on the name alone and clear ownership out from under the live session. That session
    then has its offer refused (the gate requires ownership) and can never release its
    own capture again -- an orphaned parec for the process lifetime.
    """
    relay = AudioRelay()
    relay.claim("lan")
    _fake_started(relay)
    stale_gen = relay.generation

    # The connection is superseded and a NEW session of the same transport takes over.
    relay.claim("lan")
    proc_new = _fake_started(relay)

    # The old flow finally resumes and tries to give its claim back.
    assert relay.release("lan", generation=stale_gen) is False, (
        "REGRESSION: a superseded flow released the token held by a newer session"
    )
    assert relay.owner == "lan", (
        "REGRESSION: the live session lost ownership; its offer would be refused and "
        "its capture could never be released"
    )

    # The live session can still release its own capture normally.
    assert relay.release_and_stop("lan", generation=relay.generation) is True
    assert proc_new.terminated
    print("PASS: a stale release cannot strip a newer session's token")


def test_start_versioned_generation_is_usable_for_release():
    """The generation a start hands back must be the one a later release matches.

    Reading the generation before the start misses the start's own bump, and reading it
    after can pick up a claim that landed in between -- either way the session can never
    release its OWN capture, so parec leaks for the process lifetime.
    """
    relay = AudioRelay()
    relay.claim("lan")
    # A capture is already running, so start_versioned takes the reuse branch -- the
    # same bump path a fresh spawn uses. Install the fake proc FIRST so nothing bumps
    # the generation after the start hands it back.
    relay._parec_proc = FakeProc()
    relay._running = True
    started, my_gen = relay.start_versioned(64000)
    assert started is True

    # Reading it BEFORE the start (the trap) would be stale by exactly one bump.
    assert relay.release("lan", generation=my_gen - 1) is False, (
        "a pre-start generation must not match"
    )
    assert relay.owner == "lan", "the failed release must not strip ownership"

    # The generation the start returned is the one that works.
    assert relay.release("lan", generation=my_gen) is True, (
        "REGRESSION: the session cannot release its own capture with the generation "
        "its start returned -- parec would leak for the process lifetime"
    )
    assert relay.owner is None
    print("PASS: start_versioned's generation is usable for release")


def test_shutdown_refuses_a_late_start():
    """After shutdown, a start arriving on the reader thread must not spawn a capture.

    The websocket reader thread is never joined, so a start can land after the final
    stop. Without a fence it spawns parec, the app exits, and that capture is orphaned
    holding the monitor source with no consumer.
    """
    relay = AudioRelay()
    relay.claim("cloud")
    proc = _fake_started(relay)

    relay.shutdown()
    assert proc.terminated, "shutdown must stop the running capture"
    assert not relay._running, "shutdown must leave no capture running"

    # A start that raced shutdown must be refused BEFORE any device work happens.
    # Make detection succeed so the only thing that can refuse is the shutdown fence.
    probed = {"n": 0}

    def _detect():
        probed["n"] += 1
        return "fake.monitor"

    relay._detect_monitor = _detect
    started, _gen = relay.start_versioned(64000)
    assert probed["n"] == 0, (
        "REGRESSION: a start after shutdown reached device detection -- the fence must "
        "refuse it before it can spawn a capture"
    )
    assert started is False, (
        "REGRESSION: a start after shutdown was accepted -- it would orphan a parec "
        "holding the monitor source for the life of the session"
    )
    assert not relay._running, "no capture may be running after shutdown"
    print("PASS: shutdown refuses a late start")

if __name__ == "__main__":
    test_shutdown_refuses_a_late_start()
    test_start_versioned_generation_is_usable_for_release()
    test_stale_release_cannot_strip_a_newer_session()
    test_stop_waiting_on_the_lock_cannot_kill_a_new_capture()
    test_claim_invalidates_an_inflight_stop()
    test_delayed_release_and_stop_respects_generation()
    test_predicate_runs_under_the_lock()
    test_start_while_running_bumps_generation()
    test_release_and_stop_is_atomic()
    test_ownership_is_exclusive()
    test_stale_stop_is_ignored()
    test_concurrent_claims_have_one_winner()
    test_failed_start_releases_the_claim()
    test_peer_death_releases_ownership()
    test_owner_gates_late_initiate()
