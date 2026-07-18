"""WebRTC peer connection manager for desktop audio/video streaming."""

import asyncio
import logging

import aioice.ice
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription

from src import config

# Disable consent freshness (RFC 7675): consent STUN checks use retransmissions=0
# through a TURN relay, so any packet loss on the relay path triggers failure.
# Both endpoints are controlled and the phone explicitly requests audio, so
# consent is unnecessary.
aioice.ice.CONSENT_FAILURES = 999999

log = logging.getLogger(__name__)


def _tune_opus_encoder_for_congested_wifi():
    """Reconfigure aiortc's Opus encoder for full-quality music/system audio
    with light loss resilience on a busy 2.4GHz link.

    This relays arbitrary system audio (music included), so quality is the
    priority. aiortc defaults to 96kbps stereo but with application 'voip'
    (speech-optimised: band-limited, aggressive processing) and no loss
    resilience. We keep the full 96kbps stereo and switch to:
      - application 'audio': music/general fidelity, NOT speech processing
      - in-band FEC: the phone reconstructs a lost frame from the next one,
        masking the short gaps a congested link produces -- cheap, no quality cost
    We deliberately do NOT enable DTX (it chops continuous/music audio) and do
    NOT lower the bitrate (that was what wrecked quality). Patched at import so
    every OpusEncoder aiortc creates picks it up; we never edit the venv files.
    """
    try:
        from aiortc.codecs import opus as _opus

        _orig_init = _opus.OpusEncoder.__init__

        def _patched_init(self):
            _orig_init(self)
            self.codec.bit_rate = 96000
            opts = dict(getattr(self.codec, "options", {}) or {})
            opts.update({
                "application": "audio",  # music/general fidelity, not speech
                "fec": "on",             # in-band forward error correction
                "packet_loss": "5",      # expect a little loss -> enables FEC
            })
            self.codec.options = opts

        _opus.OpusEncoder.__init__ = _patched_init
        log.info("Opus encoder tuned for quality (96kbps stereo, audio mode, FEC)")
    except Exception as e:
        log.warning("Could not tune Opus encoder: %s", e)


_tune_opus_encoder_for_congested_wifi()


def _build_ice_servers():
    """Assemble ICE servers from environment config.

    A public STUN server is always included. A TURN server is added only when
    TURN_URL is set; credentials are optional. With no TURN configured the peer
    relies on STUN/direct connectivity.
    """
    servers = [RTCIceServer(urls=[config.STUN_URL])]
    if config.TURN_URL:
        kwargs = {"urls": [config.TURN_URL]}
        if config.TURN_USERNAME:
            kwargs["username"] = config.TURN_USERNAME
        if config.TURN_CREDENTIAL:
            kwargs["credential"] = config.TURN_CREDENTIAL
        servers.append(RTCIceServer(**kwargs))
    return servers


ICE_SERVERS = _build_ice_servers()


class DesktopWebRTCPeer:
    """Manages a single WebRTC peer connection for streaming to the phone.

    aiortc gathers all ICE candidates before createOffer/createAnswer returns,
    bundling them into the SDP. No trickle ICE is used on the desktop side.
    """

    def __init__(self, stream_id, send_signaling, on_closed=None, ice_servers=None):
        """
        Args:
            stream_id: Stream session ID from orchestrator
            send_signaling: Callback to send signaling JSON via main WS.
                           Called with (msg_type, payload_dict).
            on_closed: Optional callback invoked when connection fails/closes.
            ice_servers: Override the ICE server list. Pass an empty list for a
                         same-LAN peer so aiortc gathers only host candidates and
                         does not block ~5s waiting for a STUN reply that a
                         local-network peer never needs.
        """
        self._stream_id = stream_id
        self._send_signaling = send_signaling
        self._on_closed = on_closed
        servers = ICE_SERVERS if ice_servers is None else ice_servers
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self._pc.on("connectionstatechange", self._on_connection_state_change)
        self._audio_track = None
        self._closed = False
        self._close_task = None
        log.info("WebRTC peer created for stream %d", stream_id)

    def _on_connection_state_change(self):
        state = self._pc.connectionState
        log.info("WebRTC connection state: %s (stream %d)", state, self._stream_id)
        if state in ("failed", "closed"):
            log.warning("WebRTC connection %s for stream %d", state, self._stream_id)
            # Explicitly close to stop the audio track (releasing its hold on the
            # shared PCM queue) and release UDP sockets. This handler already runs
            # on the event loop, so schedule close() as a task on the running loop
            # rather than going through async_bridge. Without this, a dead peer's
            # track keeps draining the shared queue and starves the live stream.
            # Hold a strong ref so the GC cannot destroy the close task mid-await.
            self._close_task = asyncio.ensure_future(self.close())
            if self._on_closed:
                self._on_closed(self)

    async def create_offer_with_audio(self, audio_track):
        """Add audio track, create SDP offer, and send it via signaling.

        aiortc gathers ICE candidates internally before returning,
        so the offer SDP already contains all candidates.
        """
        self._audio_track = audio_track
        self._pc.addTrack(audio_track)
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        sdp = self._pc.localDescription.sdp
        # Log ICE candidates in the offer for debugging
        for line in sdp.splitlines():
            if line.startswith("a=candidate"):
                log.info("Offer candidate: %s", line)
        self._send_signaling("webrtc_offer", {
            "streamId": self._stream_id,
            "sdp": sdp,
        })
        log.info("WebRTC offer sent for stream %d", self._stream_id)

    async def set_answer(self, sdp):
        """Set remote SDP answer from the phone."""
        # Log ICE candidates in the answer for debugging
        for line in sdp.splitlines():
            if line.startswith("a=candidate"):
                log.info("Answer candidate: %s", line)
        answer = RTCSessionDescription(sdp=sdp, type="answer")
        await self._pc.setRemoteDescription(answer)
        log.info("WebRTC answer set for stream %d", self._stream_id)

    async def close(self):
        """Close the peer connection and release all UDP sockets. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._audio_track:
            self._audio_track.stop()
        try:
            await self._pc.close()
        except Exception as e:
            log.warning("WebRTC peer close error (stream %d): %s", self._stream_id, e)
        log.info("WebRTC peer closed for stream %d", self._stream_id)
