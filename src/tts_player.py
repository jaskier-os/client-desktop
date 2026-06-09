"""Queue-based TTS audio player using QAudioSink."""

import base64
import logging
import threading
from collections import deque

import numpy as np

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, QByteArray, QIODevice, QBuffer
from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

log = logging.getLogger(__name__)

WAV_HEADER_SIZE = 44


class TtsPlayer(QObject):
    """Plays TTS audio chunks sequentially with interrupt support."""

    sig_playback_started = pyqtSignal()
    sig_playback_finished = pyqtSignal()
    sig_interrupted = pyqtSignal(str)  # request_id
    sig_tts_amplitude = pyqtSignal(float)  # 0.0-1.0 RMS amplitude

    # Internal signal to trigger playback on the main thread
    _sig_play_next = pyqtSignal()
    # Thread-safe interrupt request signal
    _sig_request_interrupt = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = deque()
        self._lock = threading.Lock()
        self._playing = False
        self._received_final = False
        self._current_request_id = None
        self._audio_sink = None
        self._audio_buffer = None
        self._sig_play_next.connect(self._play_next)
        self._sig_request_interrupt.connect(self.interrupt)

        # Audio level emission for visualization
        self._level_samples = []
        self._level_idx = 0
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(50)
        self._level_timer.timeout.connect(self._emit_level)

    def enqueue(self, request_id, audio_base64, is_final):
        """Add a TTS audio chunk to the playback queue."""
        try:
            raw = base64.b64decode(audio_base64)
        except Exception as e:
            log.error("Failed to decode audio base64: %s", e)
            return

        # Strip WAV header to get raw PCM
        if len(raw) <= WAV_HEADER_SIZE:
            log.warning("Audio chunk too small (%d bytes), skipping", len(raw))
            return

        pcm_data = raw[WAV_HEADER_SIZE:]

        with self._lock:
            self._queue.append((request_id, pcm_data, is_final))
            if is_final:
                self._received_final = True

        if not self._playing:
            self._playing = True
            self._sig_play_next.emit()

    def request_interrupt(self):
        """Thread-safe: request interrupt from any thread via signal."""
        self._sig_request_interrupt.emit()

    def interrupt(self):
        """Stop current playback and clear the queue."""
        request_id = self._current_request_id

        self._level_timer.stop()
        self.sig_tts_amplitude.emit(0.0)

        with self._lock:
            self._queue.clear()

        if self._audio_sink:
            self._audio_sink.stop()
            self._audio_sink = None

        if self._audio_buffer:
            self._audio_buffer.close()
            self._audio_buffer = None

        self._playing = False
        self._received_final = False
        self._current_request_id = None

        if request_id:
            log.info("TTS playback interrupted for %s", request_id)
            self.sig_interrupted.emit(request_id)

    def _play_next(self):
        """Play the next chunk from the queue (runs on main thread)."""
        chunk = None
        with self._lock:
            if self._queue:
                chunk = self._queue.popleft()

        if chunk is None:
            if self._received_final:
                self._playing = False
                self._received_final = False
                self._current_request_id = None
                self.sig_playback_finished.emit()
            else:
                self._playing = False
            return

        request_id, pcm_data, is_final = chunk

        if self._current_request_id is None:
            self._current_request_id = request_id
            self.sig_playback_started.emit()

        # Set up audio format: 24kHz, 16-bit, mono (Kokoro default)
        fmt = QAudioFormat()
        fmt.setSampleRate(24000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        device = QMediaDevices.defaultAudioOutput()
        self._audio_sink = QAudioSink(device, fmt)

        self._audio_buffer = QBuffer()
        self._audio_buffer.setData(QByteArray(pcm_data))
        self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly)

        self._audio_sink.setVolume(1.0)
        self._audio_sink.stateChanged.connect(self._on_state_changed)
        self._audio_sink.start(self._audio_buffer)

        # Compute and emit audio levels for visualization
        self._level_samples = self._compute_levels(pcm_data)
        self._level_idx = 0
        log.debug("TTS levels computed: %d samples, range [%.3f, %.3f], pcm_bytes=%d",
                   len(self._level_samples),
                   min(self._level_samples) if self._level_samples else 0,
                   max(self._level_samples) if self._level_samples else 0,
                   len(pcm_data))
        self._level_timer.start()

    def _on_state_changed(self, state):
        """Handle audio sink state changes."""
        from PyQt6.QtMultimedia import QAudio
        if state == QAudio.State.IdleState:
            self._level_timer.stop()
            if self._audio_sink:
                self._audio_sink.stop()
                self._audio_sink = None
            if self._audio_buffer:
                self._audio_buffer.close()
                self._audio_buffer = None
            self._sig_play_next.emit()

    def _compute_levels(self, pcm_data):
        """Compute RMS levels for 50ms windows of PCM data."""
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
        window_size = 1200  # 50ms at 24kHz
        levels = []
        for i in range(0, len(samples), window_size):
            w = samples[i:i + window_size]
            if len(w) > 0:
                rms = float(np.sqrt(np.mean(w ** 2)))
                levels.append(min(1.0, rms * 12.0))
        return levels if levels else [0.0]

    def _emit_level(self):
        """Emit the next audio level sample."""
        if self._level_idx < len(self._level_samples):
            level = self._level_samples[self._level_idx]
            if self._level_idx % 5 == 0:
                log.debug("TTS amplitude [%d/%d]: %.3f", self._level_idx, len(self._level_samples), level)
            self.sig_tts_amplitude.emit(level)
            self._level_idx += 1
        else:
            self._level_timer.stop()
