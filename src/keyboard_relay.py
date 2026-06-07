"""Virtual keyboard relay using xdotool for remote keyboard input."""

import logging
import queue
import subprocess
import threading

log = logging.getLogger(__name__)


class KeyboardRelay:
    """Receives text from remote WebSocket and types it via xdotool.

    handle_event() is non-blocking -- it enqueues text for a background
    writer thread so the WebSocket receiver thread is never stalled by
    subprocess calls.
    """

    def __init__(self):
        self._queue = queue.Queue(maxsize=50)
        self._thread = None
        self._running = False

    def start(self):
        """Check xdotool availability and start writer thread."""
        try:
            subprocess.run(['xdotool', 'version'], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            log.warning("xdotool not found, keyboard relay unavailable")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="keyboard-relay")
        self._thread.start()
        log.info("Keyboard relay started")
        return True

    def stop(self):
        """Stop writer thread."""
        self._running = False
        if self._thread:
            # Drain queue to make room for sentinel (same pattern as mouse_relay)
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._queue.put_nowait(None)  # sentinel
            except queue.Full:
                pass
            self._thread.join(timeout=2)
            self._thread = None
        log.info("Keyboard relay stopped")

    def handle_event(self, text):
        """Enqueue text to type. Non-blocking, called from WS receiver thread."""
        if not self._running:
            return

        try:
            self._queue.put_nowait(text)
        except queue.Full:
            log.warning("Keyboard relay queue full, dropping event")

    def _writer_loop(self):
        """Background thread: dequeue text and type via xdotool."""
        log.info("[keyboard] Writer loop started")
        while self._running:
            try:
                text = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if text is None:
                break

            self._type_text(text)

        log.info("[keyboard] Writer loop exited")

    def _type_text(self, text):
        """Type text using xdotool."""
        try:
            subprocess.run(
                ['xdotool', 'type', '--clearmodifiers', '--', text],
                capture_output=True,
                timeout=10
            )
        except subprocess.TimeoutExpired:
            log.warning("xdotool type timed out")
        except Exception as e:
            log.error("Keyboard relay error: %s", e)
