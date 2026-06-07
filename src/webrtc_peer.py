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

    def __init__(self, stream_id, send_signaling, on_closed=None):
        """
        Args:
            stream_id: Stream session ID from orchestrator
            send_signaling: Callback to send signaling JSON via main WS.
                           Called with (msg_type, payload_dict).
            on_closed: Optional callback invoked when connection fails/closes.
        """
        self._stream_id = stream_id
        self._send_signaling = send_signaling
        self._on_closed = on_closed
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=ICE_SERVERS))
        self._pc.on("connectionstatechange", self._on_connection_state_change)
        self._audio_track = None
        log.info("WebRTC peer created for stream %d", stream_id)

    def _on_connection_state_change(self):
        state = self._pc.connectionState
        log.info("WebRTC connection state: %s (stream %d)", state, self._stream_id)
        if state in ("failed", "closed"):
            log.warning("WebRTC connection %s for stream %d", state, self._stream_id)
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
        """Close the peer connection."""
        if self._audio_track:
            self._audio_track.stop()
        await self._pc.close()
        log.info("WebRTC peer closed for stream %d", self._stream_id)
