"""Streaming transcription using faster-whisper with periodic re-transcription."""

import io
import logging
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import requests

log = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    words: list = field(default_factory=list)
    language: str = ""


class Transcriber:
    """Periodically re-transcribes a growing audio buffer using faster-whisper or remote service."""

    def __init__(self, model_name, device, compute_type, remote=False, remote_url="", api_key=""):
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._remote = remote
        self._remote_url = remote_url
        self._api_key = api_key
        self._model = None
        self._running = False
        self._thread = None
        self._audio_fn = None
        self._on_transcription = None
        self._last_text = ""

    def preload(self):
        if self._remote:
            log.info("Using remote transcription at %s", self._remote_url)
            return
        from faster_whisper import WhisperModel
        log.info("Loading whisper model '%s' on %s (%s)...", self._model_name, self._device, self._compute_type)
        self._model = WhisperModel(
            self._model_name, device=self._device, compute_type=self._compute_type,
            local_files_only=True,
        )
        log.info("Whisper model loaded.")

    def set_transcription_callback(self, callback):
        """callback(result: TranscriptionResult) -- called on each transcription update."""
        self._on_transcription = callback

    def start(self, audio_fn):
        """Begin periodic transcription. audio_fn() returns current np.ndarray float32."""
        self._audio_fn = audio_fn
        self._running = True
        self._last_text = ""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def finalize(self, audio):
        """Run one final transcription on complete audio."""
        if len(audio) == 0:
            return TranscriptionResult(text="")
        if self._remote:
            return self._transcribe_remote(audio)
        if self._model is None:
            return TranscriptionResult(text="")
        return self._transcribe(audio)

    def _loop(self):
        while self._running:
            time.sleep(1.0)
            if not self._running:
                break
            if self._remote:
                continue
            audio = self._audio_fn()
            if len(audio) < 1600:  # < 100ms
                continue
            result = self._transcribe(audio)
            if result.text and result.text != self._last_text:
                self._last_text = result.text
                if self._on_transcription:
                    self._on_transcription(result)

    def _transcribe_remote(self, audio):
        """Send audio to remote transcription service."""
        try:
            pcm = (audio * 32767).astype(np.int16)
            wav_buf = io.BytesIO()
            num_channels = 1
            sample_rate = 16000
            bits_per_sample = 16
            byte_rate = sample_rate * num_channels * bits_per_sample // 8
            block_align = num_channels * bits_per_sample // 8
            data_size = pcm.nbytes
            wav_buf.write(b"RIFF")
            wav_buf.write(struct.pack("<I", 36 + data_size))
            wav_buf.write(b"WAVE")
            wav_buf.write(b"fmt ")
            wav_buf.write(struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample))
            wav_buf.write(b"data")
            wav_buf.write(struct.pack("<I", data_size))
            wav_buf.write(pcm.tobytes())
            wav_buf.seek(0)

            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            resp = requests.post(
                self._remote_url,
                files={"audio": ("recording.wav", wav_buf, "audio/wav")},
                headers=headers,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("text", "").strip()
            lang = data.get("language", "")
            log.info("Remote transcription: '%s' (lang=%s)", text, lang)
            return TranscriptionResult(text=text, language=lang)
        except requests.exceptions.HTTPError as e:
            log.error("Remote transcription HTTP error: %s", e)
            return TranscriptionResult(text=self._last_text)
        except Exception as e:
            log.error("Remote transcription error: %s", e)
            return TranscriptionResult(text=self._last_text)

    def _transcribe(self, audio):
        try:
            segments, info = self._model.transcribe(
                audio,
                beam_size=5,
                word_timestamps=True,
                language=None,
                vad_filter=False,
            )
            words = []
            texts = []
            for seg in segments:
                texts.append(seg.text.strip())
                if seg.words:
                    for w in seg.words:
                        words.append({"word": w.word.strip(), "start": w.start, "end": w.end})

            text = " ".join(texts)
            return TranscriptionResult(
                text=text,
                words=words,
                language=info.language if info else "",
            )
        except Exception as e:
            log.error("Transcription error: %s", e)
            return TranscriptionResult(text=self._last_text)
