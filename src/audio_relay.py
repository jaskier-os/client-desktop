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
        # start()/stop() are called from both the Qt thread and async executor
        # threads. Without this lock two concurrent starts each spawn a parec and
        # the first is orphaned for the process lifetime (its capture thread keeps
        # racing the second one on the same queue).
        self._lock = threading.RLock()
        # Which transport currently owns the capture ("cloud" / "lan" / None). A single
        # token is the only reliable arbiter: the two transports otherwise each check
        # the other's "am I streaming" predicate, and both can answer False during the
        # seconds-wide window between deciding to start and installing their peer --
        # so both attach a track and split the 50fps queue (halved frame rate).
        self._owner = None
        self._generation = 0
        # Ownership is guarded by its OWN lock, never _lock. _lock is held across
        # parec terminate+join (up to ~5s), and claim() runs on the asyncio loop --
        # sharing one lock would stall ICE/DTLS/keepalives behind a teardown.
        self._owner_lock = threading.Lock()

    @property
    def owner(self):
        return self._owner

    def claim(self, owner):
        """Claim capture ownership. Returns the previous owner, if any.

        Bumps the generation so any teardown scheduled before this claim is refused.
        Owner identity alone is not enough: the SAME transport can stop and immediately
        restart, and the in-flight stop would otherwise still match (nothing had bumped
        the generation yet because start() has not run) and kill the new capture.
        """
        with self._owner_lock:
            previous = self._owner
            self._owner = owner
            self._generation += 1
            return previous

    def release_and_stop_if(self, owner, predicate):
        """Release+stop only if `owner` holds the capture AND `predicate()` is True.

        The predicate runs UNDER the lock so a caller that must consult external state
        (e.g. "is a peer streaming?") cannot be overtaken between checking and acting --
        which would kill the capture of the session that appeared in that gap.
        """
        with self._owner_lock:
            if self._owner != owner or not predicate():
                return False
            self._owner = None
        with self._lock:
            self._stop_locked()
        return True

    def release(self, owner):
        """Release ownership if still held by `owner`. Returns True if released."""
        with self._owner_lock:
            if self._owner == owner:
                self._owner = None
                return True
            return False

    def release_and_stop(self, owner, generation=None):
        """Atomically release ownership and stop the capture if `owner` still held it.

        Doing this as two steps lets the other transport claim and start in between,
        and the stop then terminates that fresh capture under a live peer -- the phone
        connects and hears pure silence with no error to trigger a retry.

        `generation` guards the delayed callers (peer-death threads, executor hops):
        the SAME owner name can legitimately belong to a newer session by the time the
        call runs, so owner identity alone is not enough. Returns True if this owner
        held the capture at that generation and it was stopped.
        """
        with self._owner_lock:
            if self._owner != owner:
                return False
            if generation is not None and generation != self._generation:
                log.info("Audio relay: ignoring stale release_and_stop (gen %s, current %s)",
                         generation, self._generation)
                return False
            self._owner = None
        # Stop outside the ownership lock so a concurrent claim() is never blocked
        # behind parec's terminate+join.
        with self._lock:
            self._stop_locked()
        return True

    @property
    def pcm_queue(self):
        """Queue of raw PCM byte chunks (each BYTES_PER_FRAME long)."""
        return self._pcm_queue

    def start(self, bitrate=64000, buffer_seconds=None):
        """Start audio capture. Returns True on success, False on failure."""
        with self._lock:
            return self._start_locked()

    def _bump_generation(self):
        """Invalidate teardowns scheduled against the previous capture instance.

        Guarded by the ownership lock because that is where the generation is read.
        """
        with self._owner_lock:
            self._generation += 1
            return self._generation

    def _start_locked(self):
        if self._running:
            # Bump even though the capture is reused: a stop captured against the OLD
            # generation may be in flight (peer death schedules it on a thread), and
            # without this it would match and terminate parec under the session that
            # just claimed it -- pure silence, no error, no retry.
            log.warning("Audio relay already running (generation now %d)", self._bump_generation())
            return True

        # _running is also cleared by the capture loop when it exits on a read error,
        # leaving parec alive. Reap that remnant first, otherwise this start would
        # Popen a second one and overwrite the handle, orphaning the old process
        # (and its thread) on the shared queue for the app's lifetime.
        if self._parec_proc is not None or self._capture_thread is not None:
            log.warning("Audio relay: reaping remnants of a previous capture before start")
            self._stop_locked()

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
        self._bump_generation()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        log.info("Audio relay started: device=%s frame=%dms",
                 self._monitor_device, self.FRAME_DURATION_MS)
        return True

    def stop(self, generation=None):
        """Stop audio capture.

        `generation` makes the stop conditional: executor-offloaded stops can land
        AFTER a newer start (the lock serializes but does not order them), and would
        then terminate the fresh parec under a live peer -- the phone connects and
        hears pure silence, with no error to trigger a retry.
        """
        with self._lock:
            if generation is not None and generation != self._generation:
                log.info("Audio relay: ignoring stale stop (gen %s, current %s)",
                         generation, self._generation)
                return
            self._stop_locked()

    @property
    def generation(self):
        """Increments on every start; identifies the current capture instance."""
        return self._generation

    def _stop_locked(self):
        # Do NOT early-return on `not _running`: the capture loop clears that flag
        # when it exits (including on a read error, with parec still alive). Gating
        # on it would skip terminate()/join() and orphan the process for the app
        # lifetime, while the next start() spawns yet another one.
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
            if self._capture_thread.is_alive():
                # Keep the handle so the next start() reaps it instead of leaving a
                # second thread racing the new one on the shared queue.
                log.warning("Audio relay: capture thread did not exit within 3s")
            else:
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
