"""Regression test: is_streaming must cover the whole offer->answer window.

`is_streaming` decides whether a transport still holds the shared audio capture.
aiortc reports connectionState == "new" for the ENTIRE window between addTrack and
the answer arriving, so a predicate gated on connecting/connected reports False for
a peer that is already holding a track. Two real bugs follow from that:

  * the other transport stops the shared capture under a live session -- the phone
    then receives silence forever (PulseAudioTrack yields SILENCE_FRAME, no error);
  * the other transport skips closing this peer and adds a second track, so both
    split the 50fps capture and each side gets 25fps.

Also covers the answer watchdog: a peer that is never answered must release the
capture instead of pinning it for the process lifetime.

Run: venv/bin/python tests/test_peer_is_streaming.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.webrtc_peer import DesktopWebRTCPeer


class FakeTrack:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _make_peer(stream_id=1):
    sent = []
    peer = DesktopWebRTCPeer(stream_id, lambda t, p: sent.append(t), ice_servers=[])
    return peer, sent


async def main():
    # 1. A fresh peer holds nothing yet.
    peer, _ = _make_peer()
    assert not peer.is_streaming, "peer with no track must not count as streaming"

    # 2. Once a track is attached it IS consuming the capture -- even though aiortc
    #    still reports connectionState "new" until the answer arrives.
    track = FakeTrack()
    peer._audio_track = track
    assert peer._pc.connectionState == "new", (
        f"expected aiortc to report 'new' pre-answer, got {peer._pc.connectionState!r}"
    )
    assert peer.is_streaming, (
        "REGRESSION: a peer holding a track during the offer->answer window reports "
        "not-streaming; the other transport would stop the shared capture under it "
        "(silent audio) or add a second track (25fps split)"
    )

    # 3. After close it must release the capture and stop its track.
    await peer.close()
    assert track.stopped, "close() must stop the audio track so it stops consuming"
    assert not peer.is_streaming, "closed peer must not pin the capture"

    # 4a. The watchdog must still notify when pc.close() SUSPENDS (real ICE/DTLS
    #     teardown does). The watchdog runs as the very task close() cancels, so a
    #     cancellation delivered inside close() must not swallow the notification --
    #     nothing else clears the peer reference or releases the capture.
    peer_s, _ = _make_peer(9)
    peer_s.ANSWER_TIMEOUT = 0.05
    peer_s._audio_track = FakeTrack()
    notified = []
    peer_s._on_closed = lambda p: notified.append(p)

    real_close = peer_s._pc.close

    async def suspending_close():
        await asyncio.sleep(0)  # where the cancellation is delivered
        return await real_close()

    peer_s._pc.close = suspending_close
    peer_s._answer_watchdog = asyncio.ensure_future(peer_s._close_if_unanswered())
    await asyncio.sleep(0.3)
    assert notified, (
        "REGRESSION: the answer watchdog was cancelled by its own close() before it "
        "could notify -- the peer reference is never cleared and the capture is never "
        "released, so parec runs with no consumer and the transport cannot restart"
    )
    assert not peer_s.is_streaming, "the timed-out peer must release the capture"

    # 4. A peer that is never answered must self-close, or it pins the capture forever.
    peer2, _ = _make_peer(2)
    peer2.ANSWER_TIMEOUT = 0.2
    peer2._audio_track = FakeTrack()
    closed = []
    peer2._on_closed = lambda p: closed.append(p)
    peer2._answer_watchdog = asyncio.ensure_future(peer2._close_if_unanswered())
    assert peer2.is_streaming, "peer holding a track should be streaming before timeout"
    await asyncio.sleep(0.5)
    assert not peer2.is_streaming, (
        "REGRESSION: an unanswered peer still holds the shared capture -- it would "
        "pin the capture for the process lifetime and starve later sessions"
    )
    assert closed, "unanswered peer must report itself closed so refs are cleared"

    # 5. An answered peer must NOT be closed by the watchdog.
    peer3, _ = _make_peer(3)
    peer3.ANSWER_TIMEOUT = 0.2
    peer3._audio_track = FakeTrack()
    peer3._answered = True
    peer3._answer_watchdog = asyncio.ensure_future(peer3._close_if_unanswered())
    await asyncio.sleep(0.5)
    assert peer3.is_streaming, "REGRESSION: watchdog closed a peer that WAS answered"
    await peer3.close()

    print("PASS: is_streaming covers offer->answer; unanswered peers release the capture")


if __name__ == "__main__":
    asyncio.run(main())
