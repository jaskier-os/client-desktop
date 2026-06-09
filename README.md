# client-desktop

Python desktop relay client. Connects to the backend orchestrator over
WebSocket and lets a remote peer drive this machine: mouse, keyboard, audio,
screen, and webcam, streamed over WebRTC. Runs as a PyQt6 tray app. Linux-
oriented (uses `evdev` for input injection).

The repo also carries a local voice-listener (speech-to-text, speaker
verification, TTS, spectrogram overlay) under `src/`. It's kept for future use
but is not wired into `main.py` — the relay is the active path.

## Run

`run.sh` creates a venv, installs deps, and starts the app:

```
./run.sh
```

Manual equivalent:

```
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py
```

## Configuration

Config lives in env vars; `.env.example` is the source of truth. Copy it to
`.env` and edit:

```
cp .env.example .env
```

The one you'll usually change is `ORCHESTRATOR_WS_URL` (use `wss://` for TLS).
Others: `DEVICE_ID`, `ORCHESTRATOR_MODEL`, optional `TLS_CERT_PATH` (CA/PEM for
self-signed wss; falls back to the system trust store if empty), and optional
`TURN_URL` / `TURN_USERNAME` / `TURN_CREDENTIAL` (empty -> STUN-only). All have
safe defaults.

## Dependencies

- Python 3.12 (versions pinned for that interpreter).
- PyQt6, aiortc + av (WebRTC), opencv-python (webcam/screen), evdev (Linux input
  injection), websocket-client, python-dotenv. See `requirements.txt`.
