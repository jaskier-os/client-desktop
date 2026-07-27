"""Regression test: eviction that no-ops must not leave two consumers.

The ownership gate and the peer install are not atomic across threads, and the other
transport evicts by PEER IDENTITY -- so a close_session()/close_webrtc_peer() landing
in that window finds the peer slot empty and does nothing. Without a re-check after
installing the peer, the losing transport attaches a track anyway and BOTH drain the
shared PCM queue: each side gets ~half the frames, which the phone hears as constant
stutter (the original 25fps-against-50fps bug).

Run: venv/bin/python tests/test_transport_handoff.py
"""

import asyncio
import json
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import local_relay_server as lrs
from src.audio_relay import AudioRelay


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
    def __init__(self):
        self.remote_address = ("192.168.0.103", 5000)
        self.sent = []
        self._queue = asyncio.Queue()

    def feed(self, msg):
        self._queue.put_nowait(json.dumps(msg))

    def finish(self):
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
    tracks = []

    relay = AudioRelay()
    # Pretend the capture is running without spawning a real parec.
    relay._running = True
    relay._generation = 1

    lrs.DesktopWebRTCPeer = FakePeer
    lrs.PulseAudioTrack = lambda q: tracks.append(q) or object()

    # The cloud steals the capture exactly while the LAN start is in its executor hop --
    # i.e. after the LAN path claimed but before it installed its peer. Its eviction
    # callback is a no-op because the LAN peer slot is still empty, which is the whole
    # point of the race.
    evictions = []

    def close_cloud():
        evictions.append("cloud closed")

    server = lrs.LocalRelayServer(relay, close_other_transport=close_cloud)

    # The steal must land AFTER the pre-offer gate and BEFORE the track is attached --
    # that is the actual unprotected window. Constructing the peer is the last step in
    # between, so claim from there.
    original_peer_cls = lrs.DesktopWebRTCPeer

    class StealingPeer(original_peer_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            relay.claim("cloud")

    lrs.DesktopWebRTCPeer = StealingPeer

    ws = FakeWS()
    handler = asyncio.create_task(server._handle(ws))
    ws.feed({"type": "identify", "deviceId": "phone-01", "deviceType": "phone"})
    ws.feed({"type": "audio_relay_start", "bitrate": 64000, "desktopBuffer": 1.0})
    await asyncio.sleep(0.3)

    assert relay.owner == "cloud", "the cloud should hold the capture in this scenario"
    assert not tracks, (
        f"REGRESSION: the LAN path attached {len(tracks)} track(s) after losing the "
        "capture -- both transports would drain the shared queue and each side would "
        "receive half the frames (audible stutter)"
    )
    assert server._peer is None, "the losing transport must not keep a peer installed"

    ws.finish()
    await asyncio.wait_for(handler, timeout=2)
    print("PASS: losing a handoff mid-start attaches no second consumer")



async def ownership_lost_after_offer_reports_error():
    """Losing only the capture (peer still ours) must tell the phone.

    Suppressing the error is correct when a NEWER peer exists -- that peer owns the
    connection and would tear itself down. But when our peer is still installed and only
    the capture moved on, nothing else will offer on this connection, so a silent bail
    leaves the phone acked and waiting for an offer that never comes.
    """
    relay = AudioRelay()
    relay._running = True
    relay._generation = 1

    # The cloud steals the capture DURING the offer await, leaving our peer installed.
    class StealDuringOffer(FakePeer):
        async def create_offer_with_audio(self, track):
            relay.claim("cloud")

    lrs.DesktopWebRTCPeer = StealDuringOffer
    lrs.PulseAudioTrack = lambda q: object()

    server = lrs.LocalRelayServer(relay)
    ws = FakeWS()
    server._ws = ws

    relay.claim("lan")
    my_gen = relay.generation

    sent = []

    async def capture(msg):
        sent.append(msg)

    server._safe_send = capture

    # Drive the REAL _create_offer so the production branch decides what to send.
    await server._create_offer(ws, my_gen)

    reasons = [m.get("reason") for m in sent]
    assert "superseded" in reasons, (
        "REGRESSION: the session was dropped for losing the capture without telling the "
        f"phone; it stays acked and waits for an offer that never arrives (sent={sent})"
    )
    assert server._peer is None, "the abandoned peer must not stay installed"
    print("PASS: losing only the capture reports an error to the phone")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(ownership_lost_after_offer_reports_error())
