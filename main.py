"""Desktop relay client: orchestrator connection + mouse/keyboard/audio relay."""

import argparse
import configparser
import logging
import os
import signal
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from src.orchestrator_client import OrchestratorClient
from src.mouse_relay import MouseRelay
from src.keyboard_relay import KeyboardRelay
from src.audio_relay import AudioRelay
from src.tray import TrayIcon
from src import async_bridge

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
        self._orchestrator.on_keyboard_event = self._keyboard_relay.handle_event
        self._orchestrator.on_audio_relay_start = self._on_audio_relay_start
        self._orchestrator.on_audio_relay_stop = self._on_audio_relay_stop
        self._orchestrator.on_audio_relay_config = self._on_audio_relay_config
        self._orchestrator._audio_relay = self._audio_relay

    def start(self):
        async_bridge.start()
        self._orchestrator.connect()
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
        self._orchestrator.disconnect()
        self._mouse_relay.stop()
        self._keyboard_relay.stop()
        self._audio_relay.stop()
        async_bridge.stop()

    def _on_audio_relay_start(self, bitrate, buffer_seconds=1.0):
        """Called when phone requests desktop audio relay."""
        log.info("Audio relay start requested: bitrate=%d buffer=%.1fs", bitrate, buffer_seconds)
        if not self._audio_relay.start(bitrate, buffer_seconds=buffer_seconds):
            log.error("Audio relay failed to start -- sending error to orchestrator")
            self._orchestrator.send_audio_relay_error("no_monitor_device")
            return
        self._orchestrator.send_audio_relay_ack(
            self._audio_relay.sample_rate,
            self._audio_relay.channels,
            bitrate,
            self._audio_relay.frame_size,
            self._audio_relay.frame_duration_ms,
        )

    def _on_audio_relay_config(self, buffer_seconds):
        """Called when phone updates audio relay config. No-op with WebRTC transport."""
        log.debug("Audio relay config update ignored (WebRTC handles buffering): buffer=%.1fs", buffer_seconds)

    def _on_audio_relay_stop(self):
        """Called when phone stops desktop audio relay."""
        log.info("Audio relay stop requested")
        self._audio_relay.stop()


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
