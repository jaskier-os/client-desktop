"""Audio relay: captures system audio via PulseAudio and provides raw PCM for WebRTC streaming."""

import logging
import queue
import subprocess
import threading
import time

log = logging.getLogger(__name__)


class AudioRelay:
    """Captures desktop system audio via PulseAudio monitor and pushes raw PCM
    into a queue for consumption by the WebRTC audio track.

    The WebRTC track (PulseAudioTrack) reads from pcm_queue and wraps each
    chunk as an av.AudioFrame. aiortc handles Opus encoding internally.
    """

    SAMPLE_RATE = 48000
    CHANNELS = 2
    FRAME_SIZE = 960  # 20ms at 48kHz
    FRAME_DURATION_MS = 20
    BYTES_PER_FRAME = FRAME_SIZE * CHANNELS * 2  # 16-bit PCM = 2 bytes per sample
    STATS_INTERVAL = 2.0
    PCM_QUEUE_MAXSIZE = 10  # ~200ms of 20ms frames -- keep small for low latency

    def __init__(self):
        self._parec_proc = None
        self._capture_thread = None
        self._running = False
        self._monitor_device = None
        self._pcm_queue = queue.Queue(maxsize=self.PCM_QUEUE_MAXSIZE)

    @property
    def pcm_queue(self):
        """Queue of raw PCM byte chunks (each BYTES_PER_FRAME long)."""
        return self._pcm_queue

    def start(self, bitrate=64000, buffer_seconds=None):
        """Start audio capture. Returns True on success, False on failure."""
        if self._running:
            log.warning("Audio relay already running")
            return True

        # Detect PulseAudio monitor device
        self._monitor_device = self._detect_monitor()
        if not self._monitor_device:
            log.error("No PulseAudio monitor device found")
            return False

        # Drain any stale data from previous session
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break

        # Start parec subprocess
        cmd = [
            "parec",
            "--device", self._monitor_device,
            "--format=s16le",
            "--rate", str(self.SAMPLE_RATE),
            "--channels", str(self.CHANNELS),
            "--latency-msec", str(self.FRAME_DURATION_MS),
        ]
        try:
            self._parec_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log.error("parec not found -- install pulseaudio-utils")
            return False

        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        log.info("Audio relay started: device=%s frame=%dms",
                 self._monitor_device, self.FRAME_DURATION_MS)
        return True

    def stop(self):
        """Stop audio capture."""
        if not self._running:
            return
        self._running = False

        if self._parec_proc:
            try:
                self._parec_proc.terminate()
                self._parec_proc.wait(timeout=2)
            except Exception:
                try:
                    self._parec_proc.kill()
                except Exception:
                    pass
            self._parec_proc = None

        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None

        # Drain queue
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break

        log.info("Audio relay stopped")

    @property
    def sample_rate(self):
        return self.SAMPLE_RATE

    @property
    def channels(self):
        return self.CHANNELS

    @property
    def frame_size(self):
        return self.FRAME_SIZE

    @property
    def frame_duration_ms(self):
        return self.FRAME_DURATION_MS

    def _detect_monitor(self):
        """Find the PulseAudio/PipeWire monitor source for the default output sink."""
        # Use default sink's monitor -- avoids picking null/dummy sinks
        try:
            result = subprocess.run(
                ["pactl", "get-default-sink"],
                capture_output=True, text=True, timeout=5
            )
            default_sink = result.stdout.strip()
            if default_sink:
                monitor = f"{default_sink}.monitor"
                # Verify it exists in source list
                sources = subprocess.run(
                    ["pactl", "list", "sources", "short"],
                    capture_output=True, text=True, timeout=5
                )
                for line in sources.stdout.strip().split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1] == monitor:
                        log.info("Using default sink monitor: %s", monitor)
                        return monitor
                log.warning("Default sink monitor '%s' not in sources, falling back", monitor)
        except Exception as e:
            log.warning("Failed to get default sink: %s", e)

        # Fallback: first monitor source
        try:
            result = subprocess.run(
                ["pactl", "list", "sources", "short"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and ".monitor" in parts[1]:
                    log.info("Fallback monitor: %s", parts[1])
                    return parts[1]
        except Exception as e:
            log.error("Failed to detect PulseAudio monitor: %s", e)
        return None

    def _capture_loop(self):
        """Read PCM from parec and push into pcm_queue for WebRTC."""
        frames_captured = 0
        last_stats = time.monotonic()

        while self._running and self._parec_proc:
            # Read one frame of PCM data
            try:
                pcm_data = self._parec_proc.stdout.read(self.BYTES_PER_FRAME)
            except Exception:
                break

            if not pcm_data or len(pcm_data) < self.BYTES_PER_FRAME:
                if self._running:
                    log.warning("parec stream ended unexpectedly")
                break

            # Push raw PCM into queue (drop oldest if full)
            try:
                self._pcm_queue.put_nowait(pcm_data)
            except queue.Full:
                # Drop oldest frame to make room
                try:
                    self._pcm_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._pcm_queue.put_nowait(pcm_data)
                except queue.Full:
                    pass

            frames_captured += 1

            # Stats logging
            now = time.monotonic()
            if now - last_stats >= self.STATS_INTERVAL:
                elapsed = now - last_stats
                log.info(
                    "Audio relay: %d frames in %.1fs (%.1f fps), queue=%d/%d",
                    frames_captured, elapsed,
                    frames_captured / elapsed if elapsed > 0 else 0,
                    self._pcm_queue.qsize(), self.PCM_QUEUE_MAXSIZE
                )
                frames_captured = 0
                last_stats = now

        self._running = False
        log.info("Audio relay capture loop ended")
