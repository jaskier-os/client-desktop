"""Webcam capture utility. Takes a single photo and returns it as base64 JPEG."""

import base64
import logging

import cv2

log = logging.getLogger(__name__)

JPEG_QUALITY = 85


def capture_photo(camera_index=0, warmup_frames=5):
    """Capture a single photo from the webcam.

    Args:
        camera_index: Camera device index (0 = default webcam).
        warmup_frames: Number of frames to discard for auto-exposure.

    Returns:
        Base64-encoded JPEG string, or None on failure.
    """
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        log.error("Failed to open webcam at index %d", camera_index)
        return None

    try:
        # Discard initial frames so auto-exposure/white-balance can settle
        for _ in range(warmup_frames):
            cap.read()

        ret, frame = cap.read()
        if not ret or frame is None:
            log.error("Failed to read frame from webcam")
            return None

        success, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not success:
            log.error("Failed to encode frame as JPEG")
            return None

        b64 = base64.b64encode(buf).decode('utf-8')
        log.info("Photo captured from webcam %d (%dx%d)", camera_index, frame.shape[1], frame.shape[0])
        return b64
    finally:
        cap.release()
