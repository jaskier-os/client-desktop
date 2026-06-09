"""Wake word detection via arecord subprocess + Vosk."""

import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager

import numpy as np
from vosk import Model, KaldiRecognizer, SetLogLevel

SetLogLevel(-1)


@contextmanager
def _suppress_native_stderr():
    """Suppress C-level stderr (Vosk/Kaldi writes directly to fd 2)."""
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def _load_model(path):
    with _suppress_native_stderr():
        return Model(path)


def _create_recognizer(model, rate, grammar):
    with _suppress_native_stderr():
        return KaldiRecognizer(model, rate, grammar)

log = logging.getLogger(__name__)

VOSK_RATE = 16000
# Read 200ms chunks at native rate, resample to 16kHz for Vosk
BLOCK_MS = 200


class MonitorMode:
    WAKE = "wake"
    TTS_INTERRUPT = "tts_interrupt"


def _find_capture_device():
    """Find the first ALSA capture device that actually has signal."""
    result = subprocess.run(
        ["arecord", "-l"], capture_output=True, text=True, timeout=5
    )
    devices = []
    for line in result.stdout.splitlines():
        # Format: "card N: NAME [DESC], device M: NAME [DESC]"
        m = re.match(r"card\s+(\d+):.+,\s*device\s+(\d+):\s*(.+)", line)
        if m:
            card, device, name = m.group(1), m.group(2), m.group(3).strip()
            hw = f"hw:{card},{device}"
            devices.append((hw, name))

    log.debug("Found %d ALSA capture devices", len(devices))

    for hw, name in devices:
        try:
            proc = subprocess.Popen(
                ["arecord", "-D", hw, "-f", "S16_LE", "-r", "48000",
                 "-c", "1", "-t", "raw", "-d", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            data = proc.stdout.read()
            proc.wait(timeout=3)
            if data:
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(samples ** 2)))
                log.debug("  %s (%s): RMS=%.6f %s", hw, name, rms,
                          "** HAS SIGNAL **" if rms > 0.001 else "")
                if rms > 0.001:
                    return hw, name
        except Exception as e:
            log.debug("  %s failed: %s", hw, e)

    if devices:
        hw, name = devices[0]
        log.warning("No device with signal found, falling back to %s", hw)
        return hw, name

    raise RuntimeError("No ALSA capture devices found. Check 'arecord -l'.")


def _resample(audio_int16, src_rate, dst_rate):
    """Resample int16 array via linear interpolation, return int16."""
    if src_rate == dst_rate:
        return audio_int16
    samples = audio_int16.astype(np.float32)
    ratio = dst_rate / src_rate
    new_len = int(len(samples) * ratio)
    if new_len == 0:
        return np.array([], dtype=np.int16)
    indices = np.arange(new_len) / ratio
    indices = np.clip(indices, 0, len(samples) - 1)
    floor_idx = indices.astype(int)
    frac = indices - floor_idx
    ceil_idx = np.minimum(floor_idx + 1, len(samples) - 1)
    resampled = samples[floor_idx] * (1 - frac) + samples[ceil_idx] * frac
    return resampled.astype(np.int16)


class AudioMonitor:
    """Auto-detects best ALSA mic, captures via arecord, feeds Vosk."""

    def __init__(self, vosk_model_en_path, vosk_model_ru_path, keywords,
                 enable_rms_gate=True, rms_threshold=0.02,
                 confidence_threshold=0.6, use_word_boundaries=True):
        self.keywords = [k.lower() for k in keywords]
        self._on_wake = None
        self._mode = MonitorMode.WAKE
        self._tts_interrupt_callback = None
        self._tts_interrupt_keywords = ["shut up", "заткнись", "замолчи", "исчезни"]
        self._running = False
        self._proc = None
        self._thread = None
        self._hw_device = None
        self._native_rate = 48000
        self._rebuild_event = threading.Event()

        # Multi-layer filtering configuration
        self._enable_rms_gate = enable_rms_gate
        self._rms_threshold = rms_threshold
        self._confidence_threshold = confidence_threshold
        self._use_word_boundaries = use_word_boundaries

        # Speaker verification gate (optional)
        self._speaker_verifier = None
        # Rolling audio buffer: 2 seconds at 16kHz for speaker verification
        self._rolling_buffer = np.zeros(VOSK_RATE * 2, dtype=np.float32)
        self._rolling_write_pos = 0
        self._rolling_filled = False

        log.info("Loading Vosk EN model: %s", vosk_model_en_path)
        self._model_en = _load_model(vosk_model_en_path)
        log.info("Loading Vosk RU model: %s", vosk_model_ru_path)
        self._model_ru = _load_model(vosk_model_ru_path)

        log.debug("Wake word filtering: RMS gate=%s (threshold=%.3f), confidence threshold=%.2f, word boundaries=%s",
                  self._enable_rms_gate, self._rms_threshold, self._confidence_threshold, self._use_word_boundaries)

    def set_speaker_verifier(self, verifier):
        """Set optional speaker verifier for wake word gating."""
        self._speaker_verifier = verifier

    def set_wake_callback(self, callback):
        """callback() -- called when wake word is detected."""
        self._on_wake = callback

    def set_mode(self, mode):
        """Switch between WAKE and TTS_INTERRUPT modes."""
        if self._mode == mode:
            return
        self._mode = mode
        self._rebuild_event.set()
        log.info("Monitor mode set to %s", mode)

    def _build_grammar(self):
        if self._mode == MonitorMode.WAKE:
            keywords = self.keywords
        else:
            keywords = self._tts_interrupt_keywords
        parts = ', '.join(f'"{kw}"' for kw in keywords)
        return f'[{parts}, "[unk]"]'

    def set_tts_interrupt_callback(self, callback):
        """callback(keyword) -- called when interrupt keyword is detected in TTS_INTERRUPT mode."""
        self._tts_interrupt_callback = callback

    def start(self):
        if not self._hw_device:
            self._hw_device, dev_name = _find_capture_device()
            log.debug("Using capture device: %s (%s)", self._hw_device, dev_name)

        self._running = True
        chunk_bytes = int(self._native_rate * BLOCK_MS / 1000) * 2  # s16le

        self._proc = subprocess.Popen(
            ["arecord", "-D", self._hw_device, "-f", "S16_LE",
             "-r", str(self._native_rate), "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._chunk_bytes = chunk_bytes
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log.debug("Listening for wake words %s on %s (pid %d)",
                  self.keywords, self._hw_device, self._proc.pid)

    def stop(self):
        self._running = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _extract_rolling_buffer(self):
        """Extract the rolling buffer as a contiguous float32 array."""
        buf = self._rolling_buffer
        pos = self._rolling_write_pos
        if self._rolling_filled:
            return np.concatenate([buf[pos:], buf[:pos]])
        else:
            return buf[:pos].copy()

    def _read_loop(self):
        last_grammar = None
        while self._running:
            grammar = self._build_grammar()
            if grammar != last_grammar:
                log.debug("Recognizer grammar: %s", grammar)
                last_grammar = grammar
            rec_en = _create_recognizer(self._model_en, VOSK_RATE, grammar)
            rec_ru = _create_recognizer(self._model_ru, VOSK_RATE, grammar)
            self._rebuild_event.clear()

            try:
                while self._running and self._proc and self._proc.poll() is None:
                    if self._rebuild_event.is_set():
                        self._rebuild_event.clear()
                        grammar = self._build_grammar()
                        last_grammar = grammar
                        log.debug("Recognizer grammar: %s", grammar)
                        rec_en = _create_recognizer(self._model_en, VOSK_RATE, grammar)
                        rec_ru = _create_recognizer(self._model_ru, VOSK_RATE, grammar)

                    data = self._proc.stdout.read(self._chunk_bytes)
                    if not data:
                        log.debug("arecord returned no data")
                        break

                    # Resample 48kHz -> 16kHz
                    raw = np.frombuffer(data, dtype=np.int16)
                    resampled = _resample(raw, self._native_rate, VOSK_RATE)

                    # Always fill rolling buffer (for speaker verification context)
                    samples_f32 = resampled.astype(np.float32) / 32768.0
                    n = len(samples_f32)
                    buf = self._rolling_buffer
                    pos = self._rolling_write_pos
                    buf_len = len(buf)
                    if pos + n <= buf_len:
                        buf[pos:pos + n] = samples_f32
                    else:
                        first = buf_len - pos
                        buf[pos:] = samples_f32[:first]
                        buf[:n - first] = samples_f32[first:]
                    self._rolling_write_pos = (pos + n) % buf_len
                    if not self._rolling_filled and self._rolling_write_pos < pos:
                        self._rolling_filled = True

                    # Layer 1: RMS noise gate
                    if self._enable_rms_gate:
                        rms = float(np.sqrt(np.mean(samples_f32 ** 2)))
                        if rms < self._rms_threshold:
                            continue  # Skip Vosk processing for low-energy audio

                    audio_bytes = resampled.tobytes()

                    for label, rec in (("en", rec_en), ("ru", rec_ru)):
                        # Only match on final Vosk results. Partial results with a
                        # constrained grammar false-positive on ambient noise constantly.
                        if not rec.AcceptWaveform(audio_bytes):
                            continue

                        result = json.loads(rec.Result())
                        text = result.get("text", "").strip()

                        if not text:
                            continue

                        # Layer 2: Confidence thresholding
                        confidence = result.get("confidence", 0.0)
                        if confidence < self._confidence_threshold:
                            log.debug("[%s] Low confidence (%.2f < %.2f): '%s'",
                                      label, confidence, self._confidence_threshold, text)
                            continue

                        lower = text.lower()
                        log.debug("[%s] confidence=%.2f: '%s'", label, confidence, text)

                        if self._mode == MonitorMode.WAKE:
                            for kw in self.keywords:
                                # Layer 3: Word boundary matching
                                if self._use_word_boundaries:
                                    pattern = r'\b' + re.escape(kw) + r'\b'
                                    matched = re.search(pattern, lower, re.IGNORECASE | re.UNICODE)
                                else:
                                    matched = kw in lower

                                if matched:
                                    # Layer 4: Speaker verification gate
                                    if self._speaker_verifier is not None:
                                        audio = self._extract_rolling_buffer()
                                        verified, similarity = self._speaker_verifier.verify(audio)
                                        if not verified:
                                            log.info("Speaker rejected (similarity=%.3f), ignoring wake word '%s' via [%s]",
                                                     similarity, kw, label)
                                            continue
                                        log.info("Speaker verified (similarity=%.3f)", similarity)
                                    log.info("Wake word '%s' detected via [%s] (confidence=%.2f)!", kw, label, confidence)
                                    if self._on_wake:
                                        self._on_wake()
                                    return

                        elif self._mode == MonitorMode.TTS_INTERRUPT:
                            for kw in self._tts_interrupt_keywords:
                                # Use word boundary matching for interrupt keywords too
                                if self._use_word_boundaries:
                                    pattern = r'\b' + re.escape(kw) + r'\b'
                                    if re.search(pattern, lower, re.IGNORECASE | re.UNICODE):
                                        log.info("Interrupt keyword '%s' detected via [%s] (confidence=%.2f)!", kw, label, confidence)
                                        if self._tts_interrupt_callback:
                                            self._tts_interrupt_callback(kw)
                                        grammar = self._build_grammar()
                                        rec_en = _create_recognizer(self._model_en, VOSK_RATE, grammar)
                                        rec_ru = _create_recognizer(self._model_ru, VOSK_RATE, grammar)
                                        break
                                else:
                                    # Fallback: substring matching
                                    if kw in lower:
                                        log.info("Interrupt keyword '%s' detected via [%s] (confidence=%.2f)!", kw, label, confidence)
                                        if self._tts_interrupt_callback:
                                            self._tts_interrupt_callback(kw)
                                        grammar = self._build_grammar()
                                        rec_en = _create_recognizer(self._model_en, VOSK_RATE, grammar)
                                        rec_ru = _create_recognizer(self._model_ru, VOSK_RATE, grammar)
                                        break

            except Exception as e:
                log.error("Monitor read loop error: %s", e)

            # Stream died unexpectedly -- restart arecord with backoff
            if not self._running:
                break
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None
            log.debug("Audio stream ended, restarting...")
            time.sleep(1.0)
            if not self._running:
                break
            self._proc = subprocess.Popen(
                ["arecord", "-D", self._hw_device, "-f", "S16_LE",
                 "-r", str(self._native_rate), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            log.debug("Monitor restarted on %s (pid %d)", self._hw_device, self._proc.pid)
