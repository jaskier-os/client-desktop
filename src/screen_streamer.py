"""FFmpeg-based screen streamer that captures X11 and sends H.264 via WebSocket."""

import logging
import os
import struct
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# NAL start code
NAL_START = b'\x00\x00\x00\x01'

# NAL types (byte & 0x1F after start code)
NAL_SPS = 7
NAL_PPS = 8
NAL_IDR = 5
NAL_P_FRAME = 1

# Binary header version
HEADER_VERSION = 0x01

# Resolutions
RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "480p": (854, 480),
}

READ_CHUNK_SIZE = 65536  # 64KB

VALID_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
VALID_PROFILES = {"baseline", "main", "high"}


def get_monitors():
    """Return list of monitor geometries [(x, y, w, h), ...] using xrandr."""
    monitors = []
    try:
        result = subprocess.run(
            ["xrandr", "--listmonitors"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n')[1:]:  # skip header
            # Format: " 0: +*HDMI-0 1920/527x1080/296+0+0  HDMI-0"
            parts = line.strip().split()
            if len(parts) >= 3:
                geom = parts[2]  # e.g. "1920/527x1080/296+0+0"
                # Remove physical size info
                geom = geom.replace('*', '')
                if '+' in geom and 'x' in geom:
                    wx_part, rest = geom.split('x', 1)
                    w = int(wx_part.split('/')[0])
                    hy_part = rest.split('+')
                    h = int(hy_part[0].split('/')[0])
                    x = int(hy_part[1]) if len(hy_part) > 1 else 0
                    y = int(hy_part[2]) if len(hy_part) > 2 else 0
                    monitors.append((x, y, w, h))
    except Exception as e:
        log.warning("Failed to enumerate monitors via xrandr: %s", e)
    return monitors


class ScreenStreamer:
    """Captures screen via FFmpeg x11grab and streams H.264 over WebSocket binary frames."""

    def __init__(self, ws_send_binary, stream_id, resolution="720p", fps=24, monitor=0,
                 preset="ultrafast", profile="baseline", keyframe_interval=2):
        self._ws_send_binary = ws_send_binary
        self._stream_id = stream_id
        self._fps = max(1, min(60, int(fps)))
        self._resolution = resolution
        self._monitor = monitor
        self._keyframe_interval = max(1, min(10, int(keyframe_interval)))
        self._monitors = get_monitors()
        self._width, self._height = RESOLUTIONS.get(resolution, (1280, 720))

        if preset not in VALID_PRESETS:
            log.warning("Invalid preset '%s', falling back to 'ultrafast'", preset)
            preset = "ultrafast"
        self._preset = preset

        if profile not in VALID_PROFILES:
            log.warning("Invalid profile '%s', falling back to 'baseline'", profile)
            profile = "baseline"
        self._profile = profile

        self._process = None
        self._read_thread = None
        self._running = False
        self._start_failed = False
        self._start_time_ms = 0

    @property
    def monitor_count(self):
        return max(len(self._monitors), 1)

    @property
    def started(self):
        return self._running and not self._start_failed

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    @property
    def fps(self):
        return self._fps

    def set_ws_send_binary(self, fn):
        """Rewire the binary send callback (used when dedicated stream WS connects)."""
        self._ws_send_binary = fn

    def start(self):
        """Launch FFmpeg subprocess and start the read loop thread."""
        display = os.environ.get("DISPLAY", ":0")

        # Determine capture region for the selected monitor
        grab_x, grab_y, grab_w, grab_h = 0, 0, 0, 0
        if self._monitors and self._monitor < len(self._monitors):
            grab_x, grab_y, grab_w, grab_h = self._monitors[self._monitor]
        elif self._monitors:
            grab_x, grab_y, grab_w, grab_h = self._monitors[0]

        if grab_w > 0 and grab_h > 0:
            video_size = f"{grab_w}x{grab_h}"
            grab_input = f"{display}+{grab_x},{grab_y}"
        else:
            video_size = None
            grab_input = display

        keyint = self._fps * self._keyframe_interval

        log.info("Starting screen streamer: stream_id=%d, monitor=%d/%d, resolution=%s (%dx%d), fps=%d, preset=%s, profile=%s, keyint=%d, grab=%s",
                 self._stream_id, self._monitor, len(self._monitors),
                 self._resolution, self._width, self._height, self._fps,
                 self._preset, self._profile, keyint, grab_input)

        cmd = [
            "ffmpeg",
            "-f", "x11grab",
            "-framerate", str(self._fps),
        ]
        if video_size:
            cmd += ["-video_size", video_size]
        cmd += [
            "-i", grab_input,
            "-vf", f"scale={self._width}:{self._height}",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", self._preset,
            "-tune", "zerolatency",
            "-profile:v", self._profile,
            "-level", "3.1",
            "-x264-params", f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0",
            "-f", "h264",
            "pipe:1",
        ]

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            log.error("ffmpeg not found -- install ffmpeg to use screen streaming")
            self._start_failed = True
            return
        except Exception as e:
            log.error("Failed to start ffmpeg: %s", e)
            self._start_failed = True
            return

        self._start_failed = False
        self._running = True
        self._start_time_ms = int(time.monotonic() * 1000)

        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

        # Log stderr in a separate thread for debugging
        threading.Thread(target=self._log_stderr, daemon=True).start()

        log.info("Screen streamer started")

    def switch_monitor(self, new_monitor):
        """Switch to a different monitor by restarting FFmpeg capture."""
        log.info("Switching from monitor %d to %d", self._monitor, new_monitor)
        self.stop()
        self._monitor = new_monitor
        self._monitors = get_monitors()
        self.start()

    def stop(self):
        """Terminate FFmpeg and stop the read loop."""
        if not self._running:
            return

        self._running = False
        log.info("Stopping screen streamer (stream_id=%d)", self._stream_id)

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("FFmpeg did not terminate gracefully, killing")
                self._process.kill()
                self._process.wait(timeout=2)
            except Exception as e:
                log.error("Error stopping ffmpeg: %s", e)
            finally:
                self._process = None

        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=3)

        log.info("Screen streamer stopped")

    def _read_loop(self):
        """Read FFmpeg stdout, split NAL units, build access units, send binary frames."""
        buffer = b''

        while self._running and self._process and self._process.poll() is None:
            try:
                chunk = self._process.stdout.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                buffer += chunk
            except Exception as e:
                if self._running:
                    log.error("Error reading ffmpeg stdout: %s", e)
                break

            # Split buffer into NAL units on start codes
            nals, buffer = self._split_nals(buffer)
            if not nals:
                continue

            # Group NALs into access units and send
            self._send_access_unit(nals)

        if self._running:
            log.info("FFmpeg process ended unexpectedly (return code: %s)",
                     self._process.returncode if self._process else "N/A")
            self._running = False

    def _split_nals(self, data):
        """Split data on NAL start codes. Returns (list of NAL payloads, remaining buffer)."""
        # Find all start code positions
        positions = []
        search_from = 0
        while True:
            pos = data.find(NAL_START, search_from)
            if pos == -1:
                break
            positions.append(pos)
            search_from = pos + 4

        if not positions:
            # No start code found -- keep buffering
            return [], data

        if len(positions) < 2:
            # Only one start code -- NAL is incomplete, keep buffering
            return [], data

        # Extract complete NALs (from one start code to the next)
        nals = []
        for i in range(len(positions) - 1):
            nals.append(data[positions[i]:positions[i + 1]])

        # Keep the last (potentially incomplete) NAL as remainder
        remaining = data[positions[-1]:]
        return nals, remaining

    def _get_nal_type(self, nal_data):
        """Extract NAL type from data starting with start code."""
        if len(nal_data) < 5:
            return -1
        # Skip the 4-byte start code, read first byte
        return nal_data[4] & 0x1F

    def _build_header(self, is_keyframe, is_config):
        """Build 10-byte binary header for a video frame."""
        flags = 0
        if is_keyframe:
            flags |= 0x01
        if is_config:
            flags |= 0x02

        timestamp_ms = int(time.monotonic() * 1000) - self._start_time_ms

        header = struct.pack('>BBII',
                             HEADER_VERSION,
                             flags,
                             self._stream_id,
                             timestamp_ms & 0xFFFFFFFF)
        return header

    def _send_access_unit(self, nals):
        """Package NAL units into a binary frame with header and send via WebSocket."""
        if not nals:
            return

        is_keyframe = False
        is_config = False

        for nal in nals:
            nal_type = self._get_nal_type(nal)
            if nal_type == NAL_IDR:
                is_keyframe = True
            elif nal_type in (NAL_SPS, NAL_PPS):
                is_config = True

        header = self._build_header(is_keyframe, is_config)
        payload = b''.join(nals)
        frame = header + payload

        try:
            self._ws_send_binary(frame)
        except Exception as e:
            if self._running:
                log.error("Failed to send video frame: %s", e)

    def _log_stderr(self):
        """Read and log FFmpeg stderr output."""
        if not self._process or not self._process.stderr:
            return
        try:
            for line in self._process.stderr:
                if not self._running:
                    break
                text = line.decode('utf-8', errors='replace').rstrip()
                if text:
                    log.debug("ffmpeg: %s", text)
        except Exception:
            pass
