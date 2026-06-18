"""Virtual mouse relay using python-evdev UInput for remote mouse control."""

import logging
import queue
import threading
from evdev import UInput, ecodes, AbsInfo

from src.screen_streamer import get_monitors

log = logging.getLogger(__name__)


class MouseRelay:
    """Creates a virtual mouse device via UInput and injects mouse events.

    handle_event() is non-blocking -- it enqueues events for a background
    writer thread so the WebSocket receiver thread is never stalled by
    kernel ioctl writes.
    """

    def __init__(self):
        self._device = None
        self._abs_device = None
        self._queue = queue.Queue(maxsize=50)
        self._thread = None
        self._stop_event = threading.Event()
        self._prev_buttons = 0
        self._prev_abs_buttons = 0
        self._monitors = []
        self._abs_max_x = 0
        self._abs_max_y = 0

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

        # Enumerate monitors and compute the virtual desktop bounding box for
        # the absolute axis ranges. Min is clamped to 0 (common case: monitors
        # are at non-negative offsets).
        self._refresh_monitor_bounds(get_monitors())

        abs_capabilities = {
            ecodes.EV_ABS: [
                (ecodes.ABS_X, AbsInfo(value=0, min=0, max=self._abs_max_x,
                                       fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_Y, AbsInfo(value=0, min=0, max=self._abs_max_y,
                                       fuzz=0, flat=0, resolution=0)),
            ],
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
        }

        try:
            self._abs_device = UInput(abs_capabilities, name="remote-desktop-abs-mouse")
            log.info("Virtual absolute mouse device created: %s", self._abs_device.devnode)
        except PermissionError:
            log.error("Permission denied creating absolute UInput device. "
                      "Ensure user is in 'input' group and /dev/uinput is accessible. "
                      "Absolute taps will not inject.")
            self._abs_device = None
        except Exception as e:
            log.error("Failed to create absolute UInput device: %s. "
                      "Absolute taps will not inject.", e)
            self._abs_device = None

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

        # Release any button still held (e.g. a press whose release was dropped/queued)
        # so closing the device never leaves a stuck button on the controlled machine.
        self._release_held_buttons()

        if self._device:
            try:
                self._device.close()
                log.info("Virtual mouse device closed")
            except Exception as e:
                log.error("Error closing UInput device: %s", e)
            finally:
                self._device = None

        if self._abs_device:
            try:
                self._abs_device.close()
                log.info("Virtual absolute mouse device closed")
            except Exception as e:
                log.error("Error closing absolute UInput device: %s", e)
            finally:
                self._abs_device = None

        self._thread = None
        self._prev_buttons = 0
        self._prev_abs_buttons = 0

    def _refresh_monitor_bounds(self, monitors):
        """Store monitor geometry and recompute the virtual-desktop abs bounds.

        Keeps the last-known-good geometry if [monitors] is empty (e.g. a transient
        xrandr hiccup) so we never wipe a working mapping. Note: the ABS axis ranges
        are fixed at device-creation time in start(); if monitors grow past the
        original bounds after start, mapped coords are clamped to the original max.
        """
        if not monitors:
            if not self._monitors:
                log.warning("No monitors enumerated, falling back to 1920x1080 abs range")
                self._abs_max_x = 1919
                self._abs_max_y = 1079
            return
        self._monitors = monitors
        self._abs_max_x = max(x + w for (x, y, w, h) in monitors) - 1
        self._abs_max_y = max(y + h for (x, y, w, h) in monitors) - 1

    def _release_held_buttons(self):
        """Write a release for any button currently held on either device, then syn.

        Defensive cleanup so a press whose release was dropped (queue full) or never
        arrived (stream torn down between press and release) cannot leave a button
        stuck down on the controlled machine.
        """
        button_codes = [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE]
        button_masks = [0x01, 0x02, 0x04]
        if self._device and self._prev_buttons:
            try:
                for mask, code in zip(button_masks, button_codes):
                    if self._prev_buttons & mask:
                        self._device.write(ecodes.EV_KEY, code, 0)
                self._device.syn()
            except Exception as e:
                log.error("[mouse] Error releasing held relative buttons: %s", e)
        if self._abs_device and self._prev_abs_buttons:
            try:
                for mask, code in zip(button_masks, button_codes):
                    if self._prev_abs_buttons & mask:
                        self._abs_device.write(ecodes.EV_KEY, code, 0)
                self._abs_device.syn()
            except Exception as e:
                log.error("[mouse] Error releasing held absolute buttons: %s", e)
        self._prev_buttons = 0
        self._prev_abs_buttons = 0

    def handle_event(self, dx, dy, buttons, scroll):
        """Enqueue a single mouse event for background processing. Non-blocking."""
        if not self._device:
            return

        try:
            self._queue.put_nowait(("rel", dx, dy, buttons, scroll))
        except queue.Full:
            pass  # drop silently -- stale mouse data is useless

    def handle_abs_event(self, monitor, norm_x, norm_y, buttons):
        """Enqueue a single absolute mouse event for background processing. Non-blocking."""
        if not self._abs_device:
            return

        try:
            self._queue.put_nowait(("abs", monitor, norm_x, norm_y, buttons))
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

            tag = item[0]
            if tag == "rel":
                _, dx, dy, buttons, scroll = item
                self._write_event(dx, dy, buttons, scroll)
            elif tag == "abs":
                _, monitor, norm_x, norm_y, buttons = item
                self._write_abs_event(monitor, norm_x, norm_y, buttons)

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

    def _write_abs_event(self, monitor, norm_x, norm_y, buttons):
        """Map a normalized per-monitor position to virtual desktop coords and write to ABS device."""
        if not self._abs_device:
            return

        mons = self._monitors
        if monitor >= len(mons):
            # Monitors may have changed since start(); re-enumerate lazily, keeping
            # last-known-good geometry and recomputing bounds if the layout changed.
            fresh = get_monitors()
            if fresh:
                self._refresh_monitor_bounds(fresh)
                mons = self._monitors
        if monitor >= len(mons):
            if not mons:
                log.warning("[mouse] No monitors available for abs event, dropping")
                return
            log.warning("[mouse] Monitor index %d out of range, falling back to monitor 0", monitor)
            monitor = 0

        gx, gy, gw, gh = mons[monitor]
        abs_x = gx + int(norm_x / 65535.0 * (gw - 1))
        abs_y = gy + int(norm_y / 65535.0 * (gh - 1))

        if abs_x < 0:
            abs_x = 0
        elif abs_x > self._abs_max_x:
            abs_x = self._abs_max_x
        if abs_y < 0:
            abs_y = 0
        elif abs_y > self._abs_max_y:
            abs_y = self._abs_max_y

        try:
            self._abs_device.write(ecodes.EV_ABS, ecodes.ABS_X, abs_x)
            self._abs_device.write(ecodes.EV_ABS, ecodes.ABS_Y, abs_y)
            self._abs_device.syn()

            # Edge-triggered button events (only on state change)
            button_map = [
                (0x01, ecodes.BTN_LEFT),
                (0x02, ecodes.BTN_RIGHT),
                (0x04, ecodes.BTN_MIDDLE),
            ]

            for mask, btn_code in button_map:
                was_pressed = bool(self._prev_abs_buttons & mask)
                is_pressed = bool(buttons & mask)
                if is_pressed and not was_pressed:
                    self._abs_device.write(ecodes.EV_KEY, btn_code, 1)  # press
                elif not is_pressed and was_pressed:
                    self._abs_device.write(ecodes.EV_KEY, btn_code, 0)  # release

            self._prev_abs_buttons = buttons

            self._abs_device.syn()
        except Exception as e:
            log.error("[mouse] Absolute UInput write error: %s", e)
