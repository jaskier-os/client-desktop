"""Regression test: a stale connection's teardown must not kill the live session.

Reproduces the reconnect loop where the phone reconnects, the new session starts
the shared AudioRelay, and then the OLD connection's finally-block teardown stops
that relay out from under the new peer -- leaving a live WebRTC peer with a dead
capture, so the phone drops and reconnects forever.

Run: venv/bin/python tests/test_local_relay_reconnect.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import local_relay_server as lrs


class FakeAudioRelay:
    SAMPLE_RATE = 48000

    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start_versioned(self, bitrate, buffer_seconds=None):
        ok = self.start(bitrate, buffer_seconds=buffer_seconds)
        self.generation += 1
        return ok, self.generation

    def start(self, bitrate, buffer_seconds=None):
        self.running = True
        self.starts += 1
        return True

    def stop(self, generation=None):
        self.running = False
        self.stops += 1

    generation = 1

    @property
    def owner(self):
        return getattr(self, "_owner", None)

    def claim(self, owner):
        prev = getattr(self, "_owner", None)
        self._owner = owner
        self.generation += 1
        return prev

    def release_and_stop(self, owner, generation=None):
        if self.release(owner, generation=generation):
            self.stop()
            return True
        return False

    def release(self, owner, generation=None):
        if generation is not None and generation != self.generation:
            return False
        if getattr(self, "_owner", None) == owner:
            self._owner = None
            return True
        return False

    @property
    def pcm_queue(self):
        return None

    sample_rate = 48000
    channels = 2
    frame_size = 960
    frame_duration_ms = 20


class FakePeer:
    def __init__(self, stream_id, send_signaling, on_closed=None, ice_servers=None):
        self.stream_id = stream_id
        self.closed = False
        self._on_closed = on_closed

    @property
    def is_streaming(self):
        return not self.closed

    async def create_offer_with_audio(self, track):
        pass

    async def close(self):
        self.closed = True
        if self._on_closed:
            self._on_closed(self)


class FakeWS:
    """Async-iterable websocket whose message delivery the test drives by hand."""

    def __init__(self, name):
        self.name = name
        self.remote_address = ("192.168.0.103", 5000)
        self.sent = []
        self._queue = asyncio.Queue()
        self._done = False

    def feed(self, msg):
        self._queue.put_nowait(json.dumps(msg))

    def finish(self):
        self._done = True
        self._queue.put_nowait(None)

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self._queue.get()
        if raw is None:
            raise StopAsyncIteration
        return raw


async def main():
    lrs.DesktopWebRTCPeer = FakePeer
    lrs.PulseAudioTrack = lambda q: object()

    relay = FakeAudioRelay()
    server = lrs.LocalRelayServer(relay)

    ws1 = FakeWS("ws1")
    ws2 = FakeWS("ws2")

    # 1. First phone connection establishes a session.
    t1 = asyncio.create_task(server._handle(ws1))
    ws1.feed({"type": "identify", "deviceId": "phone-01", "deviceType": "phone"})
    ws1.feed({"type": "audio_relay_start", "bitrate": 64000, "desktopBuffer": 1.0})
    await asyncio.sleep(0.1)
    assert relay.running, "session 1 should have the relay running"

    # 2. Phone reconnects (new TCP conn) BEFORE the old handler unwinds.
    t2 = asyncio.create_task(server._handle(ws2))
    ws2.feed({"type": "identify", "deviceId": "phone-01", "deviceType": "phone"})
    ws2.feed({"type": "audio_relay_start", "bitrate": 64000, "desktopBuffer": 1.0})
    await asyncio.sleep(0.1)
    assert relay.running, "session 2 should have the relay running"
    peer_after_start = server._peer
    assert peer_after_start is not None, "session 2 should have a live peer"

    # 3. A message still in flight on the OLD connection must be ignored. The
    #    phone sends audio_relay_stop for the old session on the old socket; if
    #    honoured it tears down the live session the same way.
    ws1.feed({"type": "audio_relay_stop"})
    await asyncio.sleep(0.1)
    assert relay.running, (
        "REGRESSION: audio_relay_stop on a stale connection tore down the live session"
    )

    # 4. Now the OLD connection finally unwinds and runs its teardown.
    ws1.finish()
    await asyncio.wait_for(t1, timeout=2)
    await asyncio.sleep(0.1)

    # The stale teardown must NOT have touched the live session.
    assert relay.running, (
        "REGRESSION: stale connection teardown stopped the live session's audio relay "
        f"(starts={relay.starts} stops={relay.stops})"
    )
    assert server._peer is peer_after_start and not peer_after_start.closed, (
        "REGRESSION: stale connection teardown closed the live session's WebRTC peer"
    )
    assert server._ws is ws2, "live websocket reference must still be ws2"

    ws2.finish()
    await asyncio.wait_for(t2, timeout=2)
    # Once the LAST connection goes away, the relay should actually stop.
    assert not relay.running, "relay should stop when the live connection closes"

    print("PASS: stale teardown and stale messages do not disturb the live session")



async def reconnect_during_ack():
    """A reconnect while the ack is in flight must not let the stale flow offer.

    _start_relay checks the socket before acking, but the ack itself yields. If the
    phone reconnects in that window the stale flow would resume in _create_offer, close
    the NEW connection's peer and then stop the capture under it -- the phone receives
    an offer and hears silence, with no error to trigger a retry.
    """
    lrs.DesktopWebRTCPeer = FakePeer
    lrs.PulseAudioTrack = lambda q: object()

    relay = FakeAudioRelay()
    server = lrs.LocalRelayServer(relay)

    ws_old = FakeWS("old")
    ws_new = FakeWS("new")

    # The ack send is where the reconnect lands.
    original_send = server._safe_send
    swapped = {"done": False}

    async def send_then_reconnect(msg):
        await original_send(msg)
        if msg.get("type") == "audio_relay_ack" and not swapped["done"]:
            swapped["done"] = True
            server._ws = ws_new  # a newer connection took over mid-ack

    server._safe_send = send_then_reconnect

    t_old = asyncio.create_task(server._handle(ws_old))
    ws_old.feed({"type": "identify", "deviceId": "phone-01", "deviceType": "phone"})
    ws_old.feed({"type": "audio_relay_start", "bitrate": 64000, "desktopBuffer": 1.0})
    await asyncio.sleep(0.2)

    assert server._peer is None, (
        "REGRESSION: the superseded flow built a peer after the phone reconnected; it "
        "would close the new session's peer and stop the capture under it"
    )
    ws_old.finish()
    await asyncio.wait_for(t_old, timeout=2)
    print("PASS: a reconnect during the ack stops the stale flow from offering")



async def reconnect_during_peer_close():
    """A reconnect while the OLD peer is closing must not build a second track.

    _create_offer re-checks the connection on entry, but replacing an existing peer
    awaits its close(). A reconnect during that await leaves the stale flow to install
    its peer over the new connection's one and attach a SECOND PulseAudioTrack to the
    shared queue -- each side then receives half the frames, and the clobbered peer has
    no reference left to close it.
    """
    lrs.DesktopWebRTCPeer = FakePeer
    tracks = []
    lrs.PulseAudioTrack = lambda q: tracks.append(q) or object()

    relay = FakeAudioRelay()
    server = lrs.LocalRelayServer(relay)

    ws_old = FakeWS("old")
    ws_new = FakeWS("new")
    server._ws = ws_old
    relay.claim("lan")

    # An existing peer whose close() yields -- the reconnect lands right there.
    class SlowClosingPeer(FakePeer):
        async def close(self):
            server._ws = ws_new  # phone reconnected while we were closing
            await asyncio.sleep(0)
            await FakePeer.close(self)

    server._peer = SlowClosingPeer(1, lambda *a: None)

    await server._create_offer(ws_old, relay.generation)

    assert not tracks, (
        f"REGRESSION: the superseded flow attached {len(tracks)} track(s) after the "
        "phone reconnected during the peer close -- two consumers would split the "
        "shared queue and each side would get half the frames"
    )
    print("PASS: a reconnect during the peer close builds no second track")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(reconnect_during_ack())
    asyncio.run(reconnect_during_peer_close())
