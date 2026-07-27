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
import threading

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

    def __init__(self, audio_relay, host="0.0.0.0", port=10101, close_other_transport=None):
        """
        Args:
            audio_relay: Shared AudioRelay (single parec capture).
            close_other_transport: Called to evict the cloud path when this one
                takes ownership of the shared capture.
        """
        self._audio_relay = audio_relay
        self._host = host
        self._port = port
        self._close_other_transport = close_other_transport or (lambda: None)
        self._server = None
        self._peer = None
        self._ws = None
        self._stream_id = 0
        # Strong refs to in-flight fire-and-forget send tasks so the GC cannot
        # destroy them mid-await.
        self._pending_tasks = set()

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
            # Claim ownership before awaiting so the connection being replaced
            # sees itself as stale and skips its own teardown.
            self._ws = ws
            await self._teardown()
            # Another connection may have arrived during that await and claimed the
            # slot; overwriting it here would strand that live connection (its messages
            # are dropped by the stale-connection guard, so it never gets an offer).
            if self._ws is not ws:
                log.info("Local relay: superseded while tearing down, dropping this connection")
                return
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
            # Only tear down if this connection still owns the session. A phone
            # reconnect installs a newer ws (and a fresh peer/capture) before this
            # handler unwinds; tearing down here would stop the shared AudioRelay
            # out from under the live session, killing audio and making the phone
            # reconnect again -- an endless reconnect loop.
            if self._ws is ws:
                self._ws = None
                await self._teardown()
            else:
                log.info("Local relay: stale connection closed, keeping live session")

    async def _on_message(self, ws, raw):
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Local relay: bad JSON from phone")
            return
        msg_type = env.get("type", "")

        # Ignore anything still buffered on a superseded connection. The phone
        # reconnects on a new socket, so a late audio_relay_stop (or a second
        # audio_relay_start) arriving on the old one would tear down or replace
        # the live session's capture and peer.
        if self._ws is not ws:
            log.debug("Local relay: ignoring %s from stale connection", msg_type)
            return

        if msg_type == "identify":
            log.info("Local relay: identify from %s (%s)", env.get("deviceId", "?"), env.get("deviceType", "?"))
        elif msg_type == "audio_relay_start":
            await self._start_relay(ws, env)
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

    async def _start_relay(self, ws, env):
        bitrate = env.get("bitrate", 64000)
        buffer_seconds = env.get("desktopBuffer", 1.0)
        log.info("Local relay: audio_relay_start bitrate=%d buffer=%.1fs", bitrate, buffer_seconds)

        # Claim the capture atomically FIRST, then evict the previous owner. Doing it in
        # this order closes the window between deciding to start and installing our peer
        # (device detection alone can take seconds), during which both transports' "is
        # the other streaming" predicates read False and both attach a track.
        previous = self._audio_relay.claim("lan")
        if previous == "cloud":
            self._close_other_transport()
        loop = asyncio.get_running_loop()
        # Take the generation FROM the start itself: reading it separately could pick up
        # a newer session's value. Every release/stop below is versioned with it, so a
        # flow that has been superseded cannot strip or stop a newer session's capture.
        started, my_gen = await loop.run_in_executor(
            None, lambda: self._audio_relay.start_versioned(bitrate, buffer_seconds=buffer_seconds)
        )
        if not started:
            log.error("Local relay: AudioRelay failed to start")
            # Give the claim back, otherwise ownership is pinned to a transport that
            # never started and no later stop can ever release the capture.
            self._audio_relay.release("lan", generation=my_gen)
            await self._safe_send({"type": "audio_relay_error", "reason": "no_monitor_device"})
            return

        # The phone may have reconnected while we were in the executor hop; _safe_send
        # targets the CURRENT socket, so acking here would answer the new connection
        # for a session it never asked for (and it would then never get an offer).
        if self._ws is not ws:
            log.info("Local relay: connection superseded during start, abandoning session")
            self._audio_relay.release("lan", generation=my_gen)
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

        # The cloud path may have taken the capture while _start_relay was suspended in
        # its executor hop. Attaching a track anyway would put two consumers on the
        # shared queue -- each transport then gets half the frames (audible stutter).
        if self._audio_relay.owner != "lan":
            owner = self._audio_relay.owner
            log.info("Local relay: skipping offer, capture owned by '%s'", owner)
            # The ack already went out, so the phone is waiting for an offer that will
            # never arrive. Tell it, or the session sits silent until its own timeout.
            await self._safe_send({"type": "audio_relay_error", "reason": "superseded"})
            if owner is None:
                # Nobody owns it, but a capture may still be running: a concurrent
                # teardown can clear ownership between our claim and our start, and the
                # start then spawns parec anyway. Nothing else would ever stop it --
                # there is no LAN-side watchdog -- so it would burn CPU with no consumer.
                loop = asyncio.get_running_loop()
                gen = self._audio_relay.generation
                await loop.run_in_executor(
                    None, lambda: self._audio_relay.stop(generation=gen)
                )
            return

        self._stream_id += 1
        stream_id = self._stream_id

        def _on_peer_closed(closed_peer):
            if self._peer is closed_peer:
                log.info("Local relay: peer closed, clearing reference for stream %d", stream_id)
                self._peer = None
                # No consumer left on this transport (e.g. the phone never answered and
                # the peer timed out). Release the capture so parec and its thread don't
                # keep running with nobody reading them.
                gen = self._audio_relay.generation
                threading.Thread(
                    target=lambda: self._audio_relay.release_and_stop("lan", generation=gen),
                    daemon=True,
                    name="audio-relay-stop",
                ).start()

        # Host-only ICE (no STUN): both peers are on the same LAN, so waiting for
        # a STUN reply just delays the offer ~5s. Empty server list -> aiortc
        # gathers host candidates immediately.
        peer = DesktopWebRTCPeer(stream_id, self._send_signaling, on_closed=_on_peer_closed, ice_servers=[])
        self._peer = peer

        # Re-verify AFTER installing the peer. The gate above and this assignment are not
        # atomic across threads, and the cloud path evicts by peer identity -- so a
        # close_session() landing in that window found _peer None and did nothing. Without
        # this re-check we would then attach a second track to the shared queue.
        if self._audio_relay.owner != "lan":
            log.info("Local relay: capture taken by '%s' during setup, dropping stream %d",
                     self._audio_relay.owner, stream_id)
            await self._safe_send({"type": "audio_relay_error", "reason": "superseded"})
            if self._peer is peer:
                self._peer = None
            await peer.close()
            return

        track = PulseAudioTrack(self._audio_relay.pcm_queue)
        try:
            await peer.create_offer_with_audio(track)
        except Exception as e:
            log.error("Local relay: create offer failed: %s", e)
            if self._peer is peer:
                self._peer = None
            await peer.close()
            # _on_peer_closed's identity guard is False now that _peer was cleared, so
            # nothing else releases the token. Leaving it held pins the capture with no
            # consumer AND makes the ownership gate refuse every later cloud initiate --
            # cloud audio would be dead until restart.
            loop = asyncio.get_running_loop()
            gen_fail = self._audio_relay.generation
            await loop.run_in_executor(
                None, lambda: self._audio_relay.release_and_stop("lan", generation=gen_fail)
            )
            return

        # We yielded during the offer; if another thread swapped or cleared the peer
        # meanwhile (close_session, a newer offer, teardown), this one is orphaned and
        # nobody holds a reference to close it -- it would stream from the shared
        # capture forever. Close it here.
        if self._peer is not peer or self._audio_relay.owner != "lan":
            log.info("Local relay: stream %d superseded during offer (owner=%s), closing orphan",
                     stream_id, self._audio_relay.owner)
            await self._safe_send({"type": "audio_relay_error", "reason": "superseded"})
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
        # release+stop must be atomic: the cloud path can claim and start in between,
        # and the stop would then kill that fresh capture under a live peer. Offloaded
        # because stop() blocks up to ~5s and this runs on the shared asyncio loop --
        # inline it would stall ICE/DTLS/WS keepalives and make the phone drop.
        loop = asyncio.get_running_loop()
        gen = self._audio_relay.generation
        stopped = await loop.run_in_executor(
            None, lambda: self._audio_relay.release_and_stop("lan", generation=gen)
        )
        if not stopped:
            log.info("Local relay: leaving shared audio capture running (owned by cloud path)")

    def close_session(self):
        """Close the LAN WebRTC peer, leaving the shared capture and server running.

        Used when the cloud path takes over: the LAN peer's audio track would
        otherwise keep consuming the shared PCM queue alongside the cloud one.
        """
        peer = self._peer
        if peer is None:
            return
        self._peer = None
        # Stop the track synchronously: the close below is asynchronous, and the other
        # transport may attach its own track before it completes.
        if peer._audio_track is not None:
            peer._audio_track.stop()

        async def _close():
            try:
                await peer.close()
            except Exception:
                pass

        async_bridge.run_coro(_close())

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
