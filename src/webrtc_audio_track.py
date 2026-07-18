"""WebRTC audio track sourced from PulseAudio system audio capture."""

import asyncio
import logging
import fractions
import time
import queue

from aiortc import MediaStreamTrack
import av

from src.audio_relay import AudioRelay

log = logging.getLogger(__name__)

SAMPLE_RATE = AudioRelay.SAMPLE_RATE
CHANNELS = AudioRelay.CHANNELS
FRAME_SIZE = AudioRelay.FRAME_SIZE
SILENCE_TIMEOUT = 1.0  # seconds before generating silence to keep RTCP alive
SILENCE_FRAME = b'\x00' * (FRAME_SIZE * CHANNELS * 2)  # 16-bit PCM silence


class PulseAudioTrack(MediaStreamTrack):
    """Yields av.AudioFrame objects from a PCM queue for WebRTC transmission.

    The audio_relay capture loop pushes raw PCM bytes into pcm_queue.
    This track reads from it and wraps each chunk as an AudioFrame.
    aiortc handles Opus encoding internally.
    """
    kind = "audio"

    def __init__(self, pcm_queue):
        super().__init__()
        self._queue = pcm_queue
        self._start_time = None
        self._frame_count = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._loop = None

    async def recv(self):
        if self._start_time is None:
            self._start_time = time.time()
            self._loop = asyncio.get_running_loop()
            # Flush the backlog that accumulated while signaling/ICE completed
            # (up to 200ms of frames). Sending it would burst-inflate the
            # phone's NetEq jitter buffer, which then spends the whole session
            # draining via audible time-compression (accelerations). Start from
            # the freshest frame instead.
            flushed = 0
            while True:
                try:
                    self._queue.get_nowait()
                    flushed += 1
                except queue.Empty:
                    break
            if flushed:
                log.info("Flushed %d stale frames (%d ms) at track start", flushed, flushed * 20)

        # Bridge threading queue.Queue to async using run_in_executor
        pcm_bytes = await self._loop.run_in_executor(
            None, self._blocking_get
        )

        # Create av.AudioFrame from raw PCM
        frame = av.AudioFrame(format="s16", layout="stereo", samples=FRAME_SIZE)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = self._time_base
        frame.pts = self._frame_count * FRAME_SIZE
        frame.planes[0].update(pcm_bytes)

        self._frame_count += 1
        return frame

    def _blocking_get(self):
        """Blocking get from the threading queue with periodic liveness check.

        After SILENCE_TIMEOUT of empty queue, returns a silence frame to keep
        the WebRTC connection alive (RTCP keepalives depend on recv() returning).
        """
        # Check liveness BEFORE reading. At 50fps the queue is essentially never
        # empty, so a stopped track that only checked liveness in the Empty
        # handler would keep pulling frames forever -- a stale peer from a
        # reconnect storm then splits the shared queue with the live one, halving
        # each consumer's frame rate (phone gets 25fps + heavy concealment).
        if self.readyState != "live":
            raise Exception("Track ended")
        waited = 0.0
        while True:
            try:
                return self._queue.get(timeout=0.1)
            except queue.Empty:
                if self.readyState != "live":
                    raise Exception("Track ended")
                waited += 0.1
                if waited >= SILENCE_TIMEOUT:
                    return SILENCE_FRAME
