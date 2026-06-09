"""On-device speaker verification using SpeechBrain ECAPA-TDNN.

Verifies that wake word audio belongs to the enrolled speaker by comparing
192-dim embeddings against a stored voice profile (.npy).
"""

import logging
import os

import numpy as np
import torch

log = logging.getLogger(__name__)


def _patch_compat():
    """Patch compatibility issues between speechbrain 1.0.x and newer deps."""
    import torchaudio
    if not hasattr(torchaudio, "list_audio_backends"):
        torchaudio.list_audio_backends = lambda: ["ffmpeg"]

    import functools
    import huggingface_hub
    import huggingface_hub.errors
    _orig = huggingface_hub.hf_hub_download

    @functools.wraps(_orig)
    def _patched(*args, **kwargs):
        kwargs.pop("use_auth_token", None)
        try:
            return _orig(*args, **kwargs)
        except huggingface_hub.errors.EntryNotFoundError as exc:
            raise ValueError(str(exc)) from exc

    huggingface_hub.hf_hub_download = _patched


class SpeakerVerifier:
    """Verifies audio against an enrolled speaker profile.

    Uses SpeechBrain ECAPA-TDNN (192-dim embeddings, VoxCeleb-trained).
    Language-independent -- works with Russian and English.
    """

    def __init__(self, profile_path, threshold=0.45):
        self._profile_path = profile_path
        self._threshold = threshold
        self._model = None
        self._profile = None

    def initialize(self):
        """Load model and voice profile. Call once at startup."""
        if not os.path.exists(self._profile_path):
            raise FileNotFoundError(f"Voice profile not found: {self._profile_path}")

        _patch_compat()
        from speechbrain.inference.speaker import EncoderClassifier

        log.info("Loading SpeechBrain ECAPA-TDNN for speaker verification...")
        self._model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            run_opts={"device": "cpu"},
        )
        log.info("ECAPA-TDNN model loaded")

        self._profile = np.load(self._profile_path)
        norm = np.linalg.norm(self._profile)
        if norm > 0:
            self._profile = self._profile / norm
        log.info("Voice profile loaded from %s (dim=%d, norm=%.4f)",
                 self._profile_path, self._profile.shape[0], norm)

    def verify(self, audio_float32):
        """Check if audio matches the enrolled speaker.

        Args:
            audio_float32: np.ndarray float32 at 16kHz

        Returns:
            (verified: bool, similarity: float)
        """
        if self._model is None or self._profile is None:
            log.warning("Speaker verifier not initialized, passing through")
            return True, 1.0

        waveform = torch.from_numpy(audio_float32).unsqueeze(0)
        embedding = self._model.encode_batch(waveform).squeeze().cpu().numpy()

        # L2-normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        similarity = float(np.dot(self._profile, embedding))
        verified = similarity >= self._threshold
        log.info("Speaker verify: similarity=%.3f threshold=%.2f verified=%s",
                 similarity, self._threshold, verified)
        return verified, similarity
