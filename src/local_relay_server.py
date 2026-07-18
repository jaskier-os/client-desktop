"""LAN-direct audio-relay signaling server.

When the phone and this desktop are on the same network, the phone finds this
server via mDNS (`_repo-relay._tcp`) and connects its audio-relay signaling
straight here instead of routing through the cloud orchestrator. This process
then plays BOTH roles the orchestrator normally splits across two hops: the
signaling switchboard AND the media endpoint.

The wire protocol is the exact phone-facing envelope set the orchestrator uses,
so the phone speaks to this server with the same messages it sends to the cloud:

  phone -> here : identify, audio_relay_start, audio_relay_stop,
                  audio_relay_config, webrtc_answer, webrtc_ice, health(ping)
  here  -> phone: audio_relay_ack, audio_relay_error, webrtc_offer, health(pong)

The cloud path forwards audio_relay_start to the desktop as a device_command,
waits for the ack, then sends webrtc_initiate to make the desktop offer. Here
that indirection collapses: on audio_relay_start we start the shared AudioRelay,
ack, and immediately create the offer.

The server owns its OWN WebRTC peer slot so it never clobbers the cloud
OrchestratorClient's peer. The AudioRelay (single parec capture) is shared;
only one path is ever active at a time because the phone picks one.
"""

import asyncio
import json
import logging

import websockets

from src import async_bridge
from src.webrtc_peer import DesktopWebRTCPeer
from src.webrtc_audio_track import PulseAudioTrack

log = logging.getLogger(__name__)


class LocalRelayServer:
    """Asyncio WebSocket server for LAN-direct audio relay.

    Runs on the shared async_bridge event loop so its WebRTC coroutines share
    the loop with the rest of aiortc. A single phone client is served at a time.
    """

    def __init__(self, audio_relay, host="0.0.0.0", port=10101, capture_in_use_elsewhere=None):
        """
        Args:
            audio_relay: Shared AudioRelay (single parec capture).
            capture_in_use_elsewhere: Optional callable returning True while the
                cloud path has a live WebRTC peer consuming the shared capture.
                Teardown then leaves the capture running so we never kill audio
                under the other transport.
        """
        self._audio_relay = audio_relay
        self._host = host
        self._port = port
        self._capture_in_use_elsewhere = capture_in_use_elsewhere or (lambda: False)
        self._server = None
        self._peer = None
        self._ws = None
        self._stream_id = 0
        # Strong refs to in-flight fire-and-forget send tasks so the GC cannot
        # destroy them mid-await.
        self._pending_tasks = set()

    @property
    def session_active(self):
        """True while a phone is connected with a live LAN WebRTC peer."""
        return self._peer is not None

    def start(self):
        """Start the server on the shared async loop (call after async_bridge.start())."""
        fut = async_bridge.run_coro(self._start())
        fut.add_done_callback(
            lambda f: f.exception() and log.error("Local relay server failed to start: %s", f.exception())
        )

    async def _start(self):
        self._server = await websockets.serve(self._handle, self._host, self._port)
        log.info("Local relay server listening on ws://%s:%d/ws/device", self._host, self._port)

    async def _handle(self, ws):
        # Serve one phone at a time. A new connection replaces any stale one.
        if self._ws is not None:
            log.info("Local relay: new phone connection, tearing down previous session")
            await self._teardown()
        self._ws = ws
        peer = ws.remote_address[0] if ws.remote_address else "?"
        log.info("Local relay: phone connected from %s", peer)
        try:
            async for raw in ws:
                await self._on_message(ws, raw)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            log.error("Local relay: connection error: %s", e)
        finally:
            log.info("Local relay: phone disconnected from %s", peer)
            await self._teardown()
            if self._ws is ws:
                self._ws = None

    async def _on_message(self, ws, raw):
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Local relay: bad JSON from phone")
            return
        msg_type = env.get("type", "")

        if msg_type == "identify":
            log.info("Local relay: identify from %s (%s)", env.get("deviceId", "?"), env.get("deviceType", "?"))
        elif msg_type == "audio_relay_start":
            await self._start_relay(env)
        elif msg_type == "audio_relay_stop":
            log.info("Local relay: audio_relay_stop")
            await self._teardown()
        elif msg_type == "audio_relay_config":
            # No-op with WebRTC transport (buffering is handled by the peer).
            pass
        elif msg_type == "webrtc_answer":
            await self._set_answer(env)
        elif msg_type == "webrtc_ice":
            # aiortc bundles candidates in the SDP; trickle ICE is ignored, matching
            # the cloud desktop path (audio is unidirectional desktop -> phone).
            pass
        elif msg_type == "health":
            if env.get("status") == "ping":
                await self._safe_send({"type": "health", "status": "pong"})
        else:
            log.debug("Local relay: ignoring message type %s", msg_type)

    async def _start_relay(self, env):
        bitrate = env.get("bitrate", 64000)
        buffer_seconds = env.get("desktopBuffer", 1.0)
        log.info("Local relay: audio_relay_start bitrate=%d buffer=%.1fs", bitrate, buffer_seconds)

        loop = asyncio.get_running_loop()
        started = await loop.run_in_executor(
            None, lambda: self._audio_relay.start(bitrate, buffer_seconds=buffer_seconds)
        )
        if not started:
            log.error("Local relay: AudioRelay failed to start")
            await self._safe_send({"type": "audio_relay_error", "reason": "no_monitor_device"})
            return

        await self._safe_send({
            "type": "audio_relay_ack",
            "sampleRate": self._audio_relay.sample_rate,
            "channels": self._audio_relay.channels,
            "bitrate": bitrate,
            "frameSize": self._audio_relay.frame_size,
            "frameDurationMs": self._audio_relay.frame_duration_ms,
        })

        # Collapse the orchestrator's ack -> webrtc_initiate step: offer immediately.
        await self._create_offer()

    async def _create_offer(self):
        # Replace any existing peer (phone re-requesting within the same connection).
        if self._peer is not None:
            old = self._peer
            self._peer = None
            await old.close()

        self._stream_id += 1
        stream_id = self._stream_id

        def _on_peer_closed(closed_peer):
            if self._peer is closed_peer:
                log.info("Local relay: peer closed, clearing reference for stream %d", stream_id)
                self._peer = None

        # Host-only ICE (no STUN): both peers are on the same LAN, so waiting for
        # a STUN reply just delays the offer ~5s. Empty server list -> aiortc
        # gathers host candidates immediately.
        peer = DesktopWebRTCPeer(stream_id, self._send_signaling, on_closed=_on_peer_closed, ice_servers=[])
        self._peer = peer
        track = PulseAudioTrack(self._audio_relay.pcm_queue)
        try:
            await peer.create_offer_with_audio(track)
        except Exception as e:
            log.error("Local relay: create offer failed: %s", e)
            if self._peer is peer:
                self._peer = None
            await peer.close()

    async def _set_answer(self, env):
        sdp = env.get("sdp", "")
        if not self._peer:
            log.warning("Local relay: webrtc_answer with no active peer")
            return
        try:
            await self._peer.set_answer(sdp)
        except Exception as e:
            log.error("Local relay: set_answer failed: %s", e)

    def _send_signaling(self, msg_type, payload):
        """Signaling callback passed to DesktopWebRTCPeer (runs on the loop thread)."""
        msg = {"type": msg_type}
        msg.update(payload)
        # Hold a strong reference to the task. Without it the event loop only
        # keeps a weak ref and the GC can destroy the task mid-await ("Task was
        # destroyed but it is pending!"), truncating the webrtc_offer send and
        # dropping the signaling WS right as ICE starts -- which collapsed the
        # whole handshake and triggered the reconnect storm.
        task = asyncio.create_task(self._safe_send(msg))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _safe_send(self, msg):
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(msg))
        except Exception as e:
            log.error("Local relay: failed to send %s: %s", msg.get("type"), e)

    async def _teardown(self):
        if self._peer is not None:
            peer = self._peer
            self._peer = None
            try:
                await peer.close()
            except Exception:
                pass
        # The AudioRelay capture is shared with the cloud path -- only stop it
        # if no other transport is actively streaming from it.
        if self._capture_in_use_elsewhere():
            log.info("Local relay: leaving shared audio capture running (in use by cloud path)")
        else:
            self._audio_relay.stop()

    def stop(self):
        """Stop the server (called on app shutdown)."""
        if self._server is None:
            return
        fut = async_bridge.run_coro(self._stop())
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    async def _stop(self):
        await self._teardown()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            log.info("Local relay server stopped")
