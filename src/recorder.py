"""Records audio via arecord subprocess with Silero VAD for end-of-speech."""

import json
import logging
import subprocess
import threading
import time

import numpy as np
import torch
from .audio_monitor import _create_recognizer

log = logging.getLogger(__name__)

NATIVE_RATE = 48000
TARGET_RATE = 16000
# VAD needs 512 samples at 16kHz. Read equivalent at native rate.
VAD_SAMPLES_16K = 512
VAD_SAMPLES_NATIVE = int(VAD_SAMPLES_16K * NATIVE_RATE / TARGET_RATE)  # 1536
VAD_BYTES = VAD_SAMPLES_NATIVE * 2  # s16le


def _resample_f32(audio_f32, src_rate, dst_rate):
    """Resample float32 array via linear interpolation."""
    if src_rate == dst_rate:
        return audio_f32
    ratio = dst_rate / src_rate
    new_len = int(len(audio_f32) * ratio)
    if new_len == 0:
        return np.array([], dtype=np.float32)
    indices = np.arange(new_len) / ratio
    indices = np.clip(indices, 0, len(audio_f32) - 1)
    floor_idx = indices.astype(int)
    frac = (indices - floor_idx).astype(np.float32)
    ceil_idx = np.minimum(floor_idx + 1, len(audio_f32) - 1)
    return audio_f32[floor_idx] * (1 - frac) + audio_f32[ceil_idx] * frac


class Recorder:
    """Captures via arecord, resamples to 16kHz, runs Silero VAD."""

    def __init__(self, silence_threshold_ms, max_recording_seconds, hw_device=None):
        self.silence_threshold_s = silence_threshold_ms / 1000.0
        self.max_recording_s = max_recording_seconds
        self._hw_device = hw_device

        self._vad_model, _ = torch.hub.load(
            "snakers4/silero-vad", "silero_vad", trust_repo=True
        )
        self._vad_model.eval()

        self._proc = None
        self._buffer = []
        self._lock = threading.Lock()
        self._recording = False
        self.speech_detected = False
        self._on_audio_chunk = None
        self._on_speech_end = None
        self._on_cancel = None
        self._cancel_active = False
        self._cancel_words = []
        self._vosk_model_en = None
        self._vosk_model_ru = None
        self._cancel_rec_en = None
        self._cancel_rec_ru = None
        self._thread = None

    def set_hw_device(self, hw_device):
        self._hw_device = hw_device

    def set_audio_chunk_callback(self, callback):
        """callback(chunk: np.ndarray float32 at 16kHz) -- for spectrogram."""
        self._on_audio_chunk = callback

    def set_speech_end_callback(self, callback):
        """callback(audio: np.ndarray float32 at 16kHz) -- when speech ends."""
        self._on_speech_end = callback

    def _build_cancel_grammar(self):
        parts = ', '.join(f'"{kw}"' for kw in self._cancel_words)
        return f'[{parts}, "[unk]"]'

    def set_cancel_detection(self, model_en, model_ru, cancel_words, callback):
        """Enable cancel word detection via Vosk during recording."""
        self._vosk_model_en = model_en
        self._vosk_model_ru = model_ru
        self._cancel_words = [w.lower() for w in cancel_words]
        self._on_cancel = callback

    def stop_cancel_detection(self):
        self._cancel_active = False

    def start(self):
        self._buffer = []
        self._recording = True
        self.speech_detected = False
        self._vad_model.reset_states()

        if self._vosk_model_en and self._on_cancel:
            self._cancel_active = True
            grammar = self._build_cancel_grammar()
            log.info("Cancel recognizer grammar: %s", grammar)
            self._cancel_rec_en = _create_recognizer(self._vosk_model_en, TARGET_RATE, grammar)
            self._cancel_rec_ru = _create_recognizer(self._vosk_model_ru, TARGET_RATE, grammar)

        if not self._hw_device:
            self._hw_device = "default"

        self._proc = subprocess.Popen(
            ["arecord", "-D", self._hw_device, "-f", "S16_LE",
             "-r", str(NATIVE_RATE), "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log.info("Recording on %s (pid %d)", self._hw_device, self._proc.pid)

    def stop(self):
        self._recording = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def get_audio(self):
        with self._lock:
            if not self._buffer:
                return np.array([], dtype=np.float32)
            return np.concatenate(self._buffer)

    def _read_loop(self):
        start_time = time.monotonic()
        last_speech_time = start_time
        cancelled = False

        try:
            while self._recording and self._proc and self._proc.poll() is None:
                data = self._proc.stdout.read(VAD_BYTES)
                if not data or len(data) < VAD_BYTES:
                    break

                # Convert to float32 and resample 48kHz -> 16kHz
                native_f32 = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                samples_16k = _resample_f32(native_f32, NATIVE_RATE, TARGET_RATE)

                with self._lock:
                    self._buffer.append(samples_16k.copy())

                if self._on_audio_chunk:
                    self._on_audio_chunk(samples_16k)

                # Cancel word detection via Vosk (only during loading phase)
                if self._cancel_active and self._cancel_rec_en:
                    audio_i16 = (samples_16k * 32768).clip(-32768, 32767).astype(np.int16)
                    cancel_bytes = audio_i16.tobytes()
                    for label, rec in (("en", self._cancel_rec_en), ("ru", self._cancel_rec_ru)):
                        # Only match on final Vosk results to avoid false positives.
                        if not rec.AcceptWaveform(cancel_bytes):
                            continue
                        text = json.loads(rec.Result()).get("text", "").strip()
                        if text:
                            lower = text.lower()
                            for cw in self._cancel_words:
                                if cw in lower:
                                    log.info("Cancel word '%s' detected via [%s]: '%s'", cw, label, text)
                                    cancelled = True
                                    break
                        if cancelled:
                            break
                    if cancelled:
                        break

                # Silero VAD on 512-sample 16kHz chunk
                try:
                    prob = self._vad_model(torch.from_numpy(samples_16k), TARGET_RATE).item()
                except Exception:
                    prob = 0.0

                if prob > 0.65:
                    last_speech_time = time.monotonic()
                    self.speech_detected = True

                now = time.monotonic()
                elapsed = now - start_time
                silence = now - last_speech_time

                if silence >= self.silence_threshold_s and elapsed > 2.0:
                    log.info("Speech ended (%.1fs silence)", silence)
                    break

                if elapsed >= self.max_recording_s:
                    log.info("Max recording duration (%.1fs)", elapsed)
                    break
        except Exception:
            log.exception("Unhandled exception in recorder read loop; reaping arecord")
        finally:
            self._recording = False
            if self._proc:
                try:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        try:
                            self._proc.wait(timeout=2)
                        except Exception:
                            pass
                except Exception:
                    log.exception("Error while terminating arecord process")
                self._proc = None

        if cancelled:
            if self._on_cancel:
                self._on_cancel()
        else:
            audio = self.get_audio()
            if self._on_speech_end and len(audio) > 0:
                self._on_speech_end(audio)
