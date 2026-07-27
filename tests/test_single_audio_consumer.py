"""Regression test: only ONE transport may consume the shared PCM capture.

The desktop shares a single parec capture (one queue) between the cloud path and the
LAN-direct path. If a cloud WebRTC peer is left alive when the LAN session starts, both
tracks pull from that queue, so each consumer gets ~half the frames. On the phone this
showed up as recv=25fps against a 50fps capture, jitterBuffer growing past 300ms and
~2.5s of every 5s synthesized (concealed) -- audible as constant lag and stutter.

Run: venv/bin/python tests/test_single_audio_consumer.py
"""

import asyncio
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import local_relay_server as lrs
from src.webrtc_audio_track import PulseAudioTrack


class FakeAudioRelay:
    def __init__(self, pcm_queue):
        self._q = pcm_queue
        self.running = False

    def start(self, bitrate, buffer_seconds=None):
        self.running = True
        return True

    def stop(self, generation=None):
        self.running = False

    generation = 1

    @property
    def owner(self):
        return getattr(self, "_owner", None)

    def claim(self, owner):
        prev = getattr(self, "_owner", None)
        self._owner = owner
        return prev

    def release_and_stop(self, owner, generation=None):
        if self.release(owner):
            self.stop()
            return True
        return False

    def release(self, owner):
        if getattr(self, "_owner", None) == owner:
            self._owner = None
            return True
        return False

    @property
    def pcm_queue(self):
        return self._q

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


def drain_track(track, out, stop_evt):
    """Consume a PulseAudioTrack's blocking queue reads on a worker thread."""
    while not stop_evt.is_set():
        try:
            out.append(track._blocking_get())
        except Exception:
            return


async def main():
    # Use the REAL PulseAudioTrack for the LAN peer so the track the production code
    # actually creates is the one measured; a stub would hide a track that keeps
    # consuming after its peer is closed.
    lan_tracks = []

    def _make_track(q):
        t = PulseAudioTrack(q)
        lan_tracks.append(t)
        return t

    lrs.DesktopWebRTCPeer = FakePeer
    lrs.PulseAudioTrack = _make_track

    FRAME = b"\x01" * (960 * 2 * 2)
    pcm = queue.Queue(maxsize=10)
    relay = FakeAudioRelay(pcm)

    # A cloud peer is already streaming: its track consumes the shared queue.
    cloud_track = PulseAudioTrack(pcm)
    cloud_frames = []
    stop_evt = threading.Event()
    t = threading.Thread(target=drain_track, args=(cloud_track, cloud_frames, stop_evt), daemon=True)
    t.start()

    # The cloud transport owns the capture, exactly as it does in production after
    # _on_audio_relay_start claims it.
    relay.claim("cloud")

    closed = {"cloud": False}

    def close_cloud():
        closed["cloud"] = True
        cloud_track.stop()  # ends the track -> its consumer thread exits

    server = lrs.LocalRelayServer(relay, close_other_transport=close_cloud)

    ws = FakeWS()
    handler = asyncio.create_task(server._handle(ws))
    ws.feed({"type": "identify", "deviceId": "phone-01", "deviceType": "phone"})
    ws.feed({"type": "audio_relay_start", "bitrate": 64000, "desktopBuffer": 1.0})
    await asyncio.sleep(0.2)

    assert closed["cloud"], (
        "REGRESSION: LAN session started without closing the cloud peer -- both tracks "
        "would split the shared capture, halving the phone's frame rate"
    )

    # With the cloud consumer gone, the LAN consumer must receive EVERY frame.
    cloud_frames.clear()
    lan_track = PulseAudioTrack(pcm)
    lan_frames = []
    lan_stop = threading.Event()
    t2 = threading.Thread(target=drain_track, args=(lan_track, lan_frames, lan_stop), daemon=True)
    t2.start()
    await asyncio.sleep(0.05)

    SENT = 50
    for _ in range(SENT):
        pcm.put(FRAME)
        await asyncio.sleep(0.002)
    await asyncio.sleep(0.3)

    lan_stop.set()
    stop_evt.set()
    lan_track.stop()

    real = [f for f in lan_frames if f == FRAME]
    assert len(real) >= SENT - 2, (
        f"REGRESSION: LAN consumer got {len(real)}/{SENT} frames -- the capture is being "
        f"split with another consumer (cloud stole {len(cloud_frames)})"
    )
    assert not cloud_frames, (
        f"REGRESSION: closed cloud track still consumed {len(cloud_frames)} frames"
    )

    ws.finish()
    await asyncio.wait_for(handler, timeout=2)
    print(f"PASS: single consumer -- LAN got {len(real)}/{SENT} frames, cloud got 0")


if __name__ == "__main__":
    asyncio.run(main())
