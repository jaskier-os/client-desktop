"""Virtual mouse relay using python-evdev UInput for remote mouse control."""

import logging
import queue
import threading
from evdev import UInput, ecodes

log = logging.getLogger(__name__)


class MouseRelay:
    """Creates a virtual mouse device via UInput and injects mouse events.

    handle_event() is non-blocking -- it enqueues events for a background
    writer thread so the WebSocket receiver thread is never stalled by
    kernel ioctl writes.
    """

    def __init__(self):
        self._device = None
        self._queue = queue.Queue(maxsize=50)
        self._thread = None
        self._stop_event = threading.Event()
        self._prev_buttons = 0

    def start(self):
        """Create UInput virtual mouse device and start writer thread."""
        capabilities = {
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
        }

        try:
            self._device = UInput(capabilities, name="remote-desktop-mouse")
            log.info("Virtual mouse device created: %s", self._device.devnode)
        except PermissionError:
            log.error("Permission denied creating UInput device. "
                      "Ensure user is in 'input' group and /dev/uinput is accessible.")
            return False
        except Exception as e:
            log.error("Failed to create UInput device: %s", e)
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="mouse-writer")
        self._thread.start()
        log.info("Mouse relay writer thread started")
        return True

    def stop(self):
        """Close the UInput device and stop writer thread."""
        self._stop_event.set()
        # Clear queue first to make room, then put sentinel to unblock writer
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                log.warning("Mouse writer thread did not exit cleanly")

        if self._device:
            try:
                self._device.close()
                log.info("Virtual mouse device closed")
            except Exception as e:
                log.error("Error closing UInput device: %s", e)
            finally:
                self._device = None

        self._thread = None
        self._prev_buttons = 0

    def handle_event(self, dx, dy, buttons, scroll):
        """Enqueue a single mouse event for background processing. Non-blocking."""
        if not self._device:
            return

        try:
            self._queue.put_nowait((dx, dy, buttons, scroll))
        except queue.Full:
            pass  # drop silently -- stale mouse data is useless

    def _writer_loop(self):
        """Background thread: dequeue events and write to UInput."""
        log.info("[mouse] Writer loop started")
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:
                break

            dx, dy, buttons, scroll = item
            self._write_event(dx, dy, buttons, scroll)

        log.info("[mouse] Writer loop exited")

    def _write_event(self, dx, dy, buttons, scroll):
        """Write a single event to UInput device."""
        if not self._device:
            return

        try:
            # Relative movement
            if dx != 0:
                self._device.write(ecodes.EV_REL, ecodes.REL_X, dx)
            if dy != 0:
                self._device.write(ecodes.EV_REL, ecodes.REL_Y, dy)

            # Scroll
            if scroll != 0:
                self._device.write(ecodes.EV_REL, ecodes.REL_WHEEL, scroll)

            # Edge-triggered button events (only on state change)
            button_map = [
                (0x01, ecodes.BTN_LEFT),
                (0x02, ecodes.BTN_RIGHT),
                (0x04, ecodes.BTN_MIDDLE),
            ]

            for mask, btn_code in button_map:
                was_pressed = bool(self._prev_buttons & mask)
                is_pressed = bool(buttons & mask)
                if is_pressed and not was_pressed:
                    self._device.write(ecodes.EV_KEY, btn_code, 1)  # press
                elif not is_pressed and was_pressed:
                    self._device.write(ecodes.EV_KEY, btn_code, 0)  # release

            self._prev_buttons = buttons

            # SYN to flush the event
            self._device.syn()
        except Exception as e:
            log.error("[mouse] UInput write error: %s", e)
