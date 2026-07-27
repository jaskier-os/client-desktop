"""Desktop relay client: orchestrator connection + mouse/keyboard/audio relay."""

import argparse
import configparser
import logging
import os
import signal
import sys
import threading

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from src.orchestrator_client import OrchestratorClient
from src.mouse_relay import MouseRelay
from src.keyboard_relay import KeyboardRelay
from src.audio_relay import AudioRelay
from src.local_relay_server import LocalRelayServer
from src.mdns_advertiser import MdnsAdvertiser
from src.tray import TrayIcon
from src import async_bridge, config

log = logging.getLogger("listener")


class ListenerApp:
    """Desktop relay: orchestrator connection + mouse/keyboard/audio relay."""

    def __init__(self, config_path):
        self.config_path = config_path
        cfg = configparser.RawConfigParser()
        cfg.read(config_path)
        self.config = cfg

        # Orchestrator
        model = cfg.get("orchestrator", "model", fallback="sonnet")

        # Mouse relay for remote desktop control
        self._mouse_relay = MouseRelay()

        # Keyboard relay for remote desktop keyboard input
        self._keyboard_relay = KeyboardRelay()

        # Audio relay for remote desktop audio streaming
        self._audio_relay = AudioRelay()

        # Orchestrator WebSocket client
        ws_url = cfg.get("orchestrator", "ws_url", fallback="ws://localhost:10001/ws/device")
        self._orchestrator = OrchestratorClient(ws_url, device_id="desktop-listener", model=model)
        self._orchestrator.on_mouse_event = self._mouse_relay.handle_event
        self._orchestrator.on_mouse_abs_event = self._mouse_relay.handle_abs_event
        self._orchestrator.on_keyboard_event = self._keyboard_relay.handle_event
        self._orchestrator.on_audio_relay_start = self._on_audio_relay_start
        self._orchestrator.on_audio_relay_stop = self._on_audio_relay_stop
        self._orchestrator.on_audio_relay_config = self._on_audio_relay_config
        self._orchestrator.on_webrtc_initiated = self._cancel_cloud_initiate_watchdog
        self._orchestrator._audio_relay = self._audio_relay

        # LAN-direct audio relay: local signaling server + mDNS advertisement.
        # Lets a same-network phone skip the cloud hop for audio streaming.
        self._local_relay = None
        self._mdns = None
        self._cloud_initiate_watchdog = None
        # Armed from the websocket reader thread, cancelled from the Qt thread at
        # shutdown -- without this lock a shutdown can be overtaken by an arm and leave
        # a Timer running past it.
        self._watchdog_lock = threading.Lock()
        self._shutting_down = False
        if config.LOCAL_RELAY_ENABLED:
            self._local_relay = LocalRelayServer(
                self._audio_relay,
                port=config.LOCAL_RELAY_PORT,
                close_other_transport=lambda: self._orchestrator.close_webrtc_peer(),
            )
            self._mdns = MdnsAdvertiser(config.LOCAL_RELAY_PORT, device_id="desktop-listener")

    def start(self):
        async_bridge.start()
        self._orchestrator.connect()
        if self._local_relay:
            self._local_relay.start()
        if self._mdns:
            self._mdns.start()
        if self._mouse_relay.start():
            log.info("Mouse relay started")
        else:
            log.warning("Mouse relay failed to start (remote mouse control unavailable)")
        if self._keyboard_relay.start():
            log.info("Keyboard relay started")
        else:
            log.warning("Keyboard relay failed to start (remote keyboard input unavailable)")
        log.info("Desktop relay active.")

    def shutdown(self):
        log.info("Shutting down...")
        with self._watchdog_lock:
            self._shutting_down = True
        if self._mdns:
            self._mdns.stop()
        if self._local_relay:
            self._local_relay.stop()
        # Close the cloud peer explicitly rather than relying on _on_close: that runs on
        # the websocket reader thread and can land after the loop is gone, leaving the
        # RTCPeerConnection and its UDP sockets open.
        self._orchestrator.close_webrtc_peer()
        self._orchestrator.disconnect()
        self._mouse_relay.stop()
        self._keyboard_relay.stop()
        self._cancel_cloud_initiate_watchdog()
        self._audio_relay.shutdown()
        async_bridge.stop()

    def _on_audio_relay_start(self, bitrate, buffer_seconds=1.0):
        """Called when phone requests desktop audio relay."""
        log.info("Audio relay start requested: bitrate=%d buffer=%.1fs", bitrate, buffer_seconds)
        # Claim the capture atomically first, then evict the previous owner (see the
        # matching comment in LocalRelayServer._start_relay).
        previous = self._audio_relay.claim("cloud")
        if previous == "lan" and self._local_relay is not None:
            log.info("Audio relay start (cloud): closing live LAN-direct session")
            self._local_relay.close_session()
        # Take the generation FROM the start: reading it beforehand would miss the
        # start's own bump, and the release below would then never match.
        started, my_gen = self._audio_relay.start_versioned(
            bitrate, buffer_seconds=buffer_seconds
        )
        if not started:
            log.error("Audio relay failed to start -- sending error to orchestrator")
            # Give the claim back, otherwise ownership is pinned to a transport that
            # never started and no later stop can ever release the capture.
            self._audio_relay.release("cloud", generation=my_gen)
            self._orchestrator.send_audio_relay_error("no_monitor_device")
            return
        self._orchestrator.send_audio_relay_ack(
            self._audio_relay.sample_rate,
            self._audio_relay.channels,
            bitrate,
            self._audio_relay.frame_size,
            self._audio_relay.frame_duration_ms,
        )
        # If the orchestrator never follows the ack with webrtc_initiate there is no
        # peer and therefore no watchdog, so the capture would run untouched forever.
        self._arm_cloud_initiate_watchdog(my_gen)

    CLOUD_INITIATE_TIMEOUT = 25.0

    def _cancel_cloud_initiate_watchdog(self):
        with self._watchdog_lock:
            if self._cloud_initiate_watchdog is not None:
                self._cloud_initiate_watchdog.cancel()
                self._cloud_initiate_watchdog = None

    def _arm_cloud_initiate_watchdog(self, armed_gen):
        def _check():
            # Still owned by the cloud with no peer => the initiate never arrived. The
            # check runs under the relay lock so an initiate landing right now cannot
            # slip in between the test and the stop and lose its capture.
            # Version it: a new session may have claimed+started since this timer was
            # armed, and it would not yet be streaming -- stopping then would kill the
            # fresh capture and the initiate that follows would be refused.
            if self._audio_relay.release_and_stop_if(
                "cloud", lambda: not self._orchestrator.is_streaming, generation=armed_gen
            ):
                log.warning("Audio relay: no webrtc_initiate within %.0fs, released capture",
                            self.CLOUD_INITIATE_TIMEOUT)

        with self._watchdog_lock:
            if self._shutting_down:
                return
            if self._cloud_initiate_watchdog is not None:
                self._cloud_initiate_watchdog.cancel()
            timer = threading.Timer(self.CLOUD_INITIATE_TIMEOUT, _check)
            timer.daemon = True
            self._cloud_initiate_watchdog = timer
            timer.start()

    def _on_audio_relay_config(self, buffer_seconds):
        """Called when phone updates audio relay config. No-op with WebRTC transport."""
        log.debug("Audio relay config update ignored (WebRTC handles buffering): buffer=%.1fs", buffer_seconds)

    def _on_audio_relay_stop(self):
        """Called when phone stops desktop audio relay (cloud path)."""
        log.info("Audio relay stop requested")
        # The capture is shared with the LAN-direct path; a cloud stop must not
        # kill audio underneath a live LAN session (the phone stops the cloud
        # session right before starting a LAN one, and the two messages travel
        # over different sockets so they can arrive in either order).
        # Always drop the cloud peer: its track would otherwise keep draining the shared
        # queue, splitting the 50fps capture so the phone receives 25fps with the rest
        # concealed -- audible as constant lag and stutter.
        self._cancel_cloud_initiate_watchdog()
        self._orchestrator.close_webrtc_peer()
        # Only stop the capture if the cloud still owns it; if the LAN path has taken
        # over, stopping here would kill audio under a live session.
        # Offload: release_and_stop holds the relay lock across terminate+join (up to ~5s)
        # and this runs on the orchestrator's websocket reader thread, which also answers
        # health pongs and carries mouse/keyboard frames -- blocking it stalls all of them.
        gen = self._audio_relay.generation
        threading.Thread(
            target=lambda: self._audio_relay.release_and_stop("cloud", generation=gen),
            daemon=True,
            name="audio-relay-stop",
        ).start()


def main():
    parser = argparse.ArgumentParser(description="Voice listener with wake word detection")
    parser.add_argument("--config", default="config.ini", help="Path to config.ini")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config_path)

    if not os.path.exists(config_path):
        log.error("Config file not found: %s", config_path)
        log.error("Copy config.ini.example to config.ini and edit it.")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    listener = ListenerApp(config_path)

    tray = TrayIcon(app, listener, listener.config_path, listener.config)
    tray.show()

    def sigint_handler(*_):
        listener.shutdown()
        app.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    listener.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
