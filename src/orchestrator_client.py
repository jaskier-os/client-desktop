"""WebSocket client for orchestrator communication."""

import json
import logging
import struct
import threading
import time
import uuid

import websocket

from src import async_bridge
from src.webrtc_peer import DesktopWebRTCPeer
from src.webrtc_audio_track import PulseAudioTrack

log = logging.getLogger(__name__)


class StreamConnection:
    """Manages a dedicated WebSocket connection for a single stream type."""

    def __init__(self, url, stream_id, stream_type, on_binary=None, sslopt=None):
        self._url = url
        self._stream_id = stream_id
        self._stream_type = stream_type
        self._on_binary = on_binary
        self._sslopt = sslopt
        self._ws = None
        self._thread = None

    def connect(self):
        """Start the WebSocket connection on a daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        """Close the WebSocket connection."""
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def send_binary(self, data):
        """Send a binary WebSocket frame."""
        if not self._ws:
            return
        try:
            self._ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            log.error("StreamConnection[%s/%d] send_binary failed: %s",
                      self._stream_type, self._stream_id, e)

    @property
    def is_connected(self):
        return self._ws is not None

    def _run(self):
        def on_open(ws):
            log.info("StreamConnection[%s/%d] connected to %s",
                     self._stream_type, self._stream_id, self._url)

        def on_message(ws, raw):
            if isinstance(raw, bytes) and self._on_binary:
                try:
                    self._on_binary(raw)
                except Exception as e:
                    log.error("StreamConnection[%s/%d] on_binary error: %s",
                              self._stream_type, self._stream_id, e)

        def on_close(ws, code, msg):
            log.info("StreamConnection[%s/%d] closed", self._stream_type, self._stream_id)

        def on_error(ws, error):
            log.error("StreamConnection[%s/%d] error: %s",
                      self._stream_type, self._stream_id, error)

        try:
            self._ws = websocket.WebSocketApp(
                self._url,
                on_open=on_open,
                on_message=on_message,
                on_close=on_close,
                on_error=on_error,
            )
            run_kwargs = {"ping_interval": 30, "ping_timeout": 10}
            if self._sslopt:
                run_kwargs["sslopt"] = self._sslopt
            self._ws.run_forever(**run_kwargs)
        except Exception as e:
            log.error("StreamConnection[%s/%d] run error: %s",
                      self._stream_type, self._stream_id, e)
        finally:
            self._ws = None


class OrchestratorClient:
    """Connects to orchestrator via WebSocket, sends requests, receives responses and TTS audio."""

    def __init__(self, ws_url, device_id="desktop-listener", model="sonnet", tls_cert_path=None):
        self.ws_url = ws_url
        self.device_id = device_id
        self.model = model
        # Optional CA/PEM cert for wss:// connections. None -> system trust /
        # plain connection. Missing/unset never crashes.
        self._tls_cert_path = tls_cert_path

        self._ws = None
        self._thread = None
        self._shutdown = False
        self._reconnect_attempt = 0
        self._max_reconnect_delay = 30.0

        self._last_request_id = None
        self._screen_streamer = None
        self._audio_relay = None
        self._webrtc_peer = None
        self._stream_connections = {}  # (streamId, streamType) -> StreamConnection
        self._base_ws_url = ws_url.rsplit('/ws/device', 1)[0]  # e.g. "ws://host:10001"

        self.on_response = None
        self.on_tts_audio = None
        self.on_connected = None
        self.on_disconnected = None
        self.on_device_command = None
        self.on_mouse_event = None
        self.on_mouse_abs_event = None
        self.on_keyboard_event = None  # callback(text: str)
        self.on_audio_relay_start = None  # callback(bitrate, buffer_seconds)
        self.on_audio_relay_stop = None   # callback()
        self.on_audio_relay_config = None  # callback(buffer_seconds)

    def _ssl_options(self):
        """Build websocket-client sslopt for wss:// connections.

        - ws://  -> returns None (plain, no TLS).
        - wss:// with a valid TLS_CERT_PATH -> trust that CA bundle.
        - wss:// without a cert -> rely on the system trust store.

        A missing/unset cert never raises; we simply fall back.
        """
        if not str(self.ws_url).lower().startswith("wss://"):
            return None
        if self._tls_cert_path:
            return {"ca_certs": self._tls_cert_path}
        return None

    def connect(self):
        """Start the WebSocket connection on a daemon thread."""
        self._shutdown = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        """Close the connection."""
        self._shutdown = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None

    @property
    def last_request_id(self):
        return self._last_request_id

    def send_request(self, text, image_base64=None):
        """Send a transcribed text request to the orchestrator.

        Retries up to 5 times with 6s intervals (~30s total).
        Returns True if sent, False if all retries exhausted.
        """
        request_id = str(uuid.uuid4())
        self._last_request_id = request_id
        msg = {"type": "request", "requestId": request_id, "text": text}
        if image_base64:
            msg["image"] = image_base64
        if self.model:
            msg["model"] = self.model
        payload = json.dumps(msg)
        max_retries = 5
        retry_interval = 6.0

        for attempt in range(1, max_retries + 1):
            if self._ws:
                try:
                    self._ws.send(payload)
                    log.info("Sent request to orchestrator on attempt %d: %s", attempt, text)
                    return True
                except Exception as e:
                    log.error("Send attempt %d/%d failed: %s", attempt, max_retries, e)
            else:
                log.warning("Send attempt %d/%d: not connected", attempt, max_retries)

            if attempt < max_retries:
                log.info("Retrying in %.0fs...", retry_interval)
                time.sleep(retry_interval)

        log.error("Failed to send request after %d attempts", max_retries)
        return False

    def send_device_response(self, request_id, command_type, image_base64=None, screen_base64=None, text=None):
        """Send a device response back to the orchestrator."""
        if not self._ws:
            log.error("Cannot send device response - not connected")
            return
        payload = {"requestId": request_id, "commandType": command_type}
        if image_base64:
            payload["imageBase64"] = image_base64
        if screen_base64:
            payload["screenBase64"] = screen_base64
        if text:
            payload["text"] = text
        msg = {"type": "device_response", "payload": payload}
        try:
            self._ws.send(json.dumps(msg))
            log.info("Sent device response for %s (%s)", request_id, command_type)
        except Exception as e:
            log.error("Failed to send device response: %s", e)

    def send_tts_interrupt(self, request_id):
        """Send a TTS interrupt message to stop audio playback on the server."""
        if not self._ws:
            return
        msg = {"type": "tts_interrupt", "requestId": request_id}
        try:
            self._ws.send(json.dumps(msg))
            log.info("Sent TTS interrupt for %s", request_id)
        except Exception as e:
            log.error("Failed to send TTS interrupt: %s", e)

    def send_abort(self, request_id):
        """Send an abort message to cancel an in-flight request."""
        if not self._ws:
            return
        msg = {"type": "abort", "requestId": request_id}
        try:
            self._ws.send(json.dumps(msg))
            log.info("Sent abort for %s", request_id)
        except Exception as e:
            log.error("Failed to send abort: %s", e)

    def send_binary(self, data):
        """Send a binary WebSocket frame (used for video streaming)."""
        if not self._ws:
            log.error("Cannot send binary frame - not connected")
            return
        try:
            self._ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            log.error("Failed to send binary frame: %s", e)

    def _check_host_reachable(self):
        """Quick TCP connect test to the orchestrator host. Returns True if reachable."""
        import socket
        from urllib.parse import urlparse
        try:
            parsed = urlparse(self.ws_url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme in ('wss', 'https') else 80)
            with socket.create_connection((host, port), timeout=3):
                return True
        except (OSError, socket.timeout):
            return False

    def _run(self):
        """Main loop: connect, listen, reconnect on failure."""
        while not self._shutdown:
            # Check if host is reachable before attempting WS connect
            if not self._check_host_reachable():
                log.info("Orchestrator host unreachable, waiting 5s before retry")
                # Poll every 5s until reachable (reset backoff since this isn't a connect failure)
                for _ in range(5):
                    if self._shutdown:
                        return
                    time.sleep(1)
                continue

            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                run_kwargs = {"ping_interval": 0, "ping_timeout": None}
                sslopt = self._ssl_options()
                if sslopt:
                    run_kwargs["sslopt"] = sslopt
                self._ws.run_forever(**run_kwargs)
            except Exception as e:
                log.error("WebSocket run error: %s", e)

            if self._shutdown:
                break

            delay = min(1.0 * (2 ** self._reconnect_attempt), self._max_reconnect_delay)
            self._reconnect_attempt += 1
            log.info("Reconnecting in %.1fs (attempt %d)", delay, self._reconnect_attempt)
            time.sleep(delay)

    def _health_ping_loop(self):
        """Send periodic application-level health pings to keep the proxy alive."""
        while not self._shutdown and self._ws:
            time.sleep(20)
            if self._ws:
                try:
                    self._ws.send(json.dumps({"type": "health", "status": "ping"}))
                except Exception:
                    break

    def _on_open(self, ws):
        log.info("Connected to orchestrator at %s", self.ws_url)
        self._reconnect_attempt = 0
        identify = {"type": "identify", "deviceId": self.device_id, "deviceType": "pc"}
        ws.send(json.dumps(identify))
        log.info("Sent identify message for %s", self.device_id)
        threading.Thread(target=self._health_ping_loop, daemon=True).start()
        if self.on_connected:
            self.on_connected()

    def _on_message(self, ws, raw):
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Failed to parse message from orchestrator")
            return

        msg_type = envelope.get("type", "")

        if msg_type == "device_command":
            request_id = envelope.get("requestId", "")
            command = envelope.get("command", {})
            log.info("Device command received [%s]: %s", request_id, command.get("type", ""))
            if self.on_device_command:
                self.on_device_command(request_id, command)
            else:
                self._handle_device_command(request_id, command)

        elif msg_type == "response":
            request_id = envelope.get("requestId", "")
            text = envelope.get("text", "")
            status = envelope.get("status", "")
            log.info("Response received [%s] (status=%s): %s", request_id, status, text)
            if self.on_response:
                self.on_response(request_id, text, status)

        elif msg_type == "tts_audio":
            request_id = envelope.get("requestId", "")
            audio_base64 = envelope.get("audioBase64", "")
            sentence_index = envelope.get("sentenceIndex", 0)
            total_sentences = envelope.get("totalSentences", 0)
            text = envelope.get("text", "")
            is_final = envelope.get("isFinal", False)
            log.debug("TTS audio: requestId=%s sentence=%d/%d", request_id, sentence_index, total_sentences)
            if self.on_tts_audio:
                self.on_tts_audio(request_id, audio_base64, sentence_index, total_sentences, text, is_final)

        elif msg_type == "health":
            status = envelope.get("status", "")
            if status == "ping":
                pong = {"type": "health", "status": "pong"}
                ws.send(json.dumps(pong))

        elif msg_type == "stream_connect":
            self._handle_stream_connect(envelope)

        elif msg_type == "webrtc_initiate":
            self._handle_webrtc_initiate(envelope)

        elif msg_type == "webrtc_answer":
            self._handle_webrtc_answer(envelope)

        elif msg_type == "webrtc_ice":
            self._handle_webrtc_ice(envelope)

        elif msg_type == "error":
            message = envelope.get("message", "unknown error")
            log.error("Server error: %s", message)

    def _handle_stream_connect(self, envelope):
        """Handle stream_connect message: open a dedicated stream WebSocket."""
        stream_id = envelope.get("streamId")
        stream_type = envelope.get("streamType")
        endpoint = envelope.get("endpoint")
        token = envelope.get("token")

        url = f"{self._base_ws_url}{endpoint}?token={token}"
        log.info("stream_connect: type=%s, id=%s, url=%s", stream_type, stream_id, url)

        on_binary = None
        if stream_type == "mouse":
            def on_binary(data):
                if len(data) == 7 and data[0] == 0x02 and self.on_mouse_event:
                    dx = struct.unpack('>h', data[1:3])[0]
                    dy = struct.unpack('>h', data[3:5])[0]
                    buttons = data[5]
                    scroll = struct.unpack('>b', data[6:7])[0]
                    self.on_mouse_event(dx, dy, buttons, scroll)
                elif len(data) == 8 and data[0] == 0x03 and self.on_mouse_abs_event:
                    monitor = data[1]
                    norm_x = struct.unpack('>H', data[2:4])[0]
                    norm_y = struct.unpack('>H', data[4:6])[0]
                    buttons = data[6]
                    self.on_mouse_abs_event(monitor, norm_x, norm_y, buttons)
        elif stream_type == "keyboard":
            on_binary = self._handle_keyboard_frame

        conn = StreamConnection(url, stream_id, stream_type, on_binary=on_binary,
                                sslopt=self._ssl_options())
        self._stream_connections[(stream_id, stream_type)] = conn
        conn.connect()

        if stream_type == "video" and self._screen_streamer:
            self._screen_streamer.set_ws_send_binary(conn.send_binary)

    def _handle_keyboard_frame(self, data):
        """Parse keyboard binary frame: [0x04] + UTF-8 text."""
        if len(data) < 2:
            return
        if data[0] != 0x04:
            return
        try:
            text = data[1:].decode('utf-8')
        except UnicodeDecodeError:
            log.warning("Invalid UTF-8 in keyboard frame")
            return
        if self.on_keyboard_event:
            self.on_keyboard_event(text)

    def _send_signaling(self, msg_type, payload):
        """Send a WebRTC signaling message via the main WebSocket."""
        if not self._ws:
            log.error("Cannot send signaling message -- not connected")
            return
        msg = {"type": msg_type}
        msg.update(payload)
        try:
            self._ws.send(json.dumps(msg))
            log.info("Sent signaling: %s", msg_type)
        except Exception as e:
            log.error("Failed to send signaling %s: %s", msg_type, e)

    def _handle_webrtc_initiate(self, envelope):
        """Handle webrtc_initiate: create peer connection, add audio track, send offer."""
        stream_id = envelope.get("streamId", 0)
        log.info("WebRTC initiate received for stream %d", stream_id)

        # If a peer exists for the SAME stream, skip (phone retry within same session).
        # If stream ID differs, it's a new session -- close old peer and create new one.
        if self._webrtc_peer:
            if self._webrtc_peer._stream_id == stream_id:
                log.info("WebRTC peer already exists for stream %d, ignoring duplicate initiate", stream_id)
                return
            log.info("New stream %d replacing old stream %d", stream_id, self._webrtc_peer._stream_id)
            old_peer = self._webrtc_peer
            self._webrtc_peer = None
            async_bridge.run_coro(old_peer.close())

        if not self._audio_relay:
            log.error("Cannot initiate WebRTC -- no audio relay available")
            return

        def _on_peer_closed(closed_peer):
            if self._webrtc_peer is closed_peer:
                log.info("WebRTC peer closed, clearing reference for stream %d", stream_id)
                self._webrtc_peer = None

        peer = DesktopWebRTCPeer(stream_id, self._send_signaling, on_closed=_on_peer_closed)
        self._webrtc_peer = peer
        audio_track = PulseAudioTrack(self._audio_relay.pcm_queue)

        def _on_offer_done(f):
            exc = f.exception()
            if exc:
                log.error("WebRTC offer failed: %s", exc)
                # Close the failed peer to release file descriptors
                if self._webrtc_peer is peer:
                    self._webrtc_peer = None
                async_bridge.run_coro(peer.close())

        fut = async_bridge.run_coro(self._webrtc_peer.create_offer_with_audio(audio_track))
        fut.add_done_callback(_on_offer_done)

    def _handle_webrtc_answer(self, envelope):
        """Handle webrtc_answer: set remote SDP answer on the peer connection."""
        sdp = envelope.get("sdp", "")
        if not self._webrtc_peer:
            log.warning("Received WebRTC answer but no peer exists")
            return
        log.info("WebRTC answer received")
        fut = async_bridge.run_coro(self._webrtc_peer.set_answer(sdp))
        fut.add_done_callback(lambda f: f.exception() and log.error("WebRTC set_answer failed: %s", f.exception()))

    def _handle_webrtc_ice(self, envelope):
        """Handle webrtc_ice: phone sends trickle ICE candidates, but aiortc
        already has all candidates bundled in the SDP answer. Safe to ignore.

        This works because audio is unidirectional (desktop -> phone). The desktop's
        offer SDP contains all its candidates. The phone only needs the desktop's
        candidates to receive media. If bidirectional audio is ever needed,
        trickle ICE from the phone must be handled properly.
        """
        log.debug("Ignoring trickle ICE candidate (aiortc uses SDP-bundled candidates)")

    def _handle_device_command(self, request_id, command):
        """Built-in handler for device commands."""
        cmd_type = command.get("type", "")
        if cmd_type == "capture_image":
            threading.Thread(
                target=self._capture_and_respond,
                args=(request_id,),
                daemon=True
            ).start()
        elif cmd_type == "capture_screen":
            threading.Thread(
                target=self._screenshot_and_respond,
                args=(request_id,),
                daemon=True
            ).start()
        elif cmd_type == "start_screen_stream":
            self._start_screen_stream(request_id, command)
        elif cmd_type == "stop_screen_stream":
            self._stop_screen_stream(request_id)
        elif cmd_type == "switch_monitor":
            monitor = command.get("monitor", 0)
            if self._screen_streamer:
                self._screen_streamer.switch_monitor(monitor)
                self.send_device_response(request_id, "switch_monitor")
            else:
                self.send_device_response(request_id, "switch_monitor", text="no active stream")
        elif cmd_type == "start_audio_relay":
            bitrate = command.get("bitrate", 64000)
            buffer_seconds = command.get("desktopBuffer", 1.0)
            log.info("Audio relay start command: bitrate=%d buffer=%.1fs", bitrate, buffer_seconds)
            if self.on_audio_relay_start:
                self.on_audio_relay_start(bitrate, buffer_seconds)
        elif cmd_type == "audio_relay_config":
            buffer_seconds = command.get("desktopBuffer", 1.0)
            log.info("Audio relay config update: buffer=%.1fs", buffer_seconds)
            if self.on_audio_relay_config:
                self.on_audio_relay_config(buffer_seconds)
        elif cmd_type == "stop_audio_relay":
            log.info("Audio relay stop command")
            if self.on_audio_relay_stop:
                self.on_audio_relay_stop()
        elif cmd_type == "confirm":
            self.send_device_response(request_id, "confirm", text="yes")
        elif cmd_type == "choose":
            options = command.get("options", [])
            first = options[0] if options else ""
            self.send_device_response(request_id, "choose", text=first)
        else:
            log.warning("Unsupported device command: %s", cmd_type)
            self.send_device_response(
                request_id, cmd_type,
                text=f"Unsupported device command: {cmd_type}. This device does not have this capability."
            )

    def _start_screen_stream(self, request_id, command):
        """Start screen streaming via FFmpeg."""
        if self._screen_streamer:
            log.warning("Screen streamer already running, stopping first")
            self._screen_streamer.stop()

        stream_id = command.get("streamId", 1)
        resolution = command.get("resolution", "720p")
        monitor = command.get("monitor", 0)
        fps = command.get("fps", 24)
        preset = command.get("preset", "ultrafast")
        profile = command.get("profile", "baseline")
        keyframe_interval = command.get("keyframeInterval", 2)

        from src.screen_streamer import ScreenStreamer
        self._screen_streamer = ScreenStreamer(
            ws_send_binary=lambda data: None,
            stream_id=stream_id,
            resolution=resolution,
            fps=fps,
            monitor=monitor,
            preset=preset,
            profile=profile,
            keyframe_interval=keyframe_interval,
        )
        self._screen_streamer.start()

        if not self._screen_streamer.started:
            log.error("FFmpeg failed to start for stream %d", stream_id)
            self._screen_streamer = None
            self.send_device_response(request_id, "start_screen_stream", text="error: ffmpeg failed to start")
            return

        # Respond with stream parameters directly in payload
        # Orchestrator expects payload.streamId, payload.width, etc.
        payload = {
            "requestId": request_id,
            "commandType": "start_screen_stream",
            "streamId": stream_id,
            "width": self._screen_streamer.width,
            "height": self._screen_streamer.height,
            "fps": self._screen_streamer.fps,
            "monitorCount": self._screen_streamer.monitor_count,
        }
        msg = {"type": "device_response", "payload": payload}
        try:
            self._ws.send(json.dumps(msg))
        except Exception as e:
            log.error("Failed to send stream response: %s", e)
        log.info("Screen stream started: stream_id=%d, %dx%d @ %d fps",
                 stream_id, self._screen_streamer.width, self._screen_streamer.height,
                 self._screen_streamer.fps)

    def _stop_screen_stream(self, request_id):
        """Stop screen streaming."""
        if self._screen_streamer:
            self._screen_streamer.stop()
            self._screen_streamer = None
            log.info("Screen stream stopped")
        self.send_device_response(request_id, "stop_screen_stream", text="stopped")

    def _capture_and_respond(self, request_id):
        """Capture webcam photo in background thread."""
        try:
            from src.webcam import capture_photo
            b64 = capture_photo()
            if b64:
                self.send_device_response(request_id, "capture_image", image_base64=b64)
            else:
                self.send_device_response(request_id, "capture_image", text="Webcam capture failed")
        except Exception as e:
            log.error("Webcam capture error: %s", e)
            self.send_device_response(request_id, "capture_image", text=f"Webcam error: {e}")

    def _screenshot_and_respond(self, request_id):
        """Capture screenshot in background thread."""
        try:
            import subprocess
            import sys
            result = subprocess.run(
                [sys.executable, "-m", "src.screenshot", "--screen", "0"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                import base64
                data = json.loads(result.stdout)
                path = data["screenshots"][0]
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                self.send_device_response(request_id, "capture_screen", screen_base64=b64)
            else:
                self.send_device_response(request_id, "capture_screen", text="Screenshot failed")
        except Exception as e:
            log.error("Screenshot error: %s", e)
            self.send_device_response(request_id, "capture_screen", text=f"Screenshot error: {e}")

    def send_audio_relay_ack(self, sample_rate, channels, bitrate, frame_size, frame_duration_ms=60):
        """Send audio relay ACK to orchestrator."""
        if not self._ws:
            return
        msg = {
            "type": "audio_relay_ack",
            "sampleRate": sample_rate,
            "channels": channels,
            "bitrate": bitrate,
            "frameSize": frame_size,
            "frameDurationMs": frame_duration_ms,
        }
        try:
            self._ws.send(json.dumps(msg))
            log.info("Sent audio relay ACK: %dHz %dch %dbps", sample_rate, channels, bitrate)
        except Exception as e:
            log.error("Failed to send audio relay ACK: %s", e)

    def send_audio_relay_error(self, reason):
        """Send audio relay error to orchestrator (forwarded to phone)."""
        if not self._ws:
            return
        msg = {"type": "audio_relay_error", "reason": reason}
        try:
            self._ws.send(json.dumps(msg))
            log.info("Sent audio relay error: %s", reason)
        except Exception as e:
            log.error("Failed to send audio relay error: %s", e)

    def _close_stream_connections(self, stream_id=None):
        """Close dedicated stream connections, optionally filtered by stream_id."""
        keys_to_remove = []
        for key, conn in self._stream_connections.items():
            if stream_id is None or key[0] == stream_id:
                conn.disconnect()
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._stream_connections[key]

    def _on_close(self, ws, close_status_code, close_msg):
        log.info("Disconnected from orchestrator")
        self._close_stream_connections()
        if self._webrtc_peer:
            old_peer = self._webrtc_peer
            self._webrtc_peer = None
            async def _close():
                try:
                    await old_peer.close()
                except Exception:
                    pass
            async_bridge.run_coro(_close())
        if self._screen_streamer:
            self._screen_streamer.stop()
            self._screen_streamer = None
        if self.on_audio_relay_stop:
            try:
                self.on_audio_relay_stop()
            except Exception:
                pass
        if self.on_disconnected:
            self.on_disconnected()

    def _on_error(self, ws, error):
        log.error("WebSocket error: %s", error)
