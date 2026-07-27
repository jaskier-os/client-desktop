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


# Interface-name prefixes that are never a real media path to the phone:
# container bridges, virtual ethernet, VPN tunnels, WiFi-Direct, Hamachi/ZeroTier.
# aiortc/aioice otherwise gathers a host candidate on EVERY interface, and the
# phone will happily pair with the VPN-tunnel candidate (which is routable via
# the Hetzner network) instead of the real LAN address -- hairpinning the audio
# out over the internet and back even when both peers are on the same WiFi.
_VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "p2p", "zt", "wg", "ham")


def _restrict_host_candidates_to_physical():
    """Make aioice gather host ICE candidates only on real physical/WiFi
    interfaces. Patched at import; affects every peer. The LAN peer then offers
    just the real LAN address, and the cloud peer still gets its srflx/TURN
    candidates (those come from STUN/TURN, not from this host enumeration)."""
    try:
        import aioice.ice as _ice
        import ifaddr

        def _physical_host_addresses(use_ipv4, use_ipv6):
            addresses = []
            for adapter in ifaddr.get_adapters():
                name = adapter.name
                name = name.lower() if isinstance(name, str) else name.decode(errors="ignore").lower()
                if name.startswith(_VIRTUAL_IFACE_PREFIXES):
                    continue
                for ip in adapter.ips:
                    if isinstance(ip.ip, str) and use_ipv4 and ip.ip != "127.0.0.1":
                        addresses.append(ip.ip)
                    elif use_ipv6 and not isinstance(ip.ip, str) and ip.ip[0] != "::1" and ip.ip[2] == 0:
                        addresses.append(ip.ip[0])
            return addresses

        _ice.get_host_addresses = _physical_host_addresses
        log.info("ICE host candidates restricted to physical interfaces (no docker/vpn/hamachi)")
    except Exception as e:
        log.warning("Could not restrict ICE host candidates: %s", e)


_restrict_host_candidates_to_physical()


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
    # How long an offered peer may wait for the phone's answer before it gives up
    # its hold on the shared audio capture.
    ANSWER_TIMEOUT = 20.0

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
        self._answered = False
        self._answer_watchdog = None
        self._closed_notified = False
        log.info("WebRTC peer created for stream %d", stream_id)

    @property
    def is_streaming(self):
        """True while this peer holds a track on the shared capture.

        Counts the whole life of the peer from addTrack until it closes: aiortc
        reports connectionState "new" for the entire offer->answer window, so
        gating on connecting/connected would treat a peer that is mid-handshake
        (but already holding a track) as idle -- letting the capture be stopped
        under it, or a second track be added alongside it.
        """
        return not self._closed and self._audio_track is not None

    def _notify_closed(self):
        """Invoke the on_closed callback at most once.

        Both the connection-state handler and the answer watchdog can reach this. A
        second invocation spawns another release/stop, and the late one can land after
        a new session claimed the capture -- killing it under a live peer.
        """
        if self._closed_notified:
            return
        self._closed_notified = True
        if self._on_closed:
            self._on_closed(self)

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
            self._notify_closed()

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
        # A phone that never answers would otherwise leave this peer holding a track
        # on the shared capture forever, pinning the capture and starving later
        # sessions. Self-close if the handshake never completes.
        self._answer_watchdog = asyncio.ensure_future(self._close_if_unanswered())

    async def _close_if_unanswered(self):
        try:
            await asyncio.sleep(self.ANSWER_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self._closed or self._answered:
            return
        log.warning("WebRTC stream %d never answered in %.0fs, closing",
                    self._stream_id, self.ANSWER_TIMEOUT)
        await self.close()
        self._notify_closed()

    async def set_answer(self, sdp):
        """Set remote SDP answer from the phone."""
        # Log ICE candidates in the answer for debugging
        for line in sdp.splitlines():
            if line.startswith("a=candidate"):
                log.info("Answer candidate: %s", line)
        answer = RTCSessionDescription(sdp=sdp, type="answer")
        # Claim the answer BEFORE awaiting: setRemoteDescription yields, and if the
        # watchdog resumes in that gap it would still see _answered False and close a
        # peer whose answer has already arrived (session silently dead, no retry).
        self._answered = True
        if self._answer_watchdog:
            self._answer_watchdog.cancel()
            self._answer_watchdog = None
        await self._pc.setRemoteDescription(answer)
        log.info("WebRTC answer set for stream %d", self._stream_id)

    async def close(self):
        """Close the peer connection and release all UDP sockets. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._answer_watchdog:
            self._answer_watchdog.cancel()
            self._answer_watchdog = None
        if self._audio_track:
            self._audio_track.stop()
        try:
            await self._pc.close()
        except Exception as e:
            log.warning("WebRTC peer close error (stream %d): %s", self._stream_id, e)
        log.info("WebRTC peer closed for stream %d", self._stream_id)
