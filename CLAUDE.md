# CLAUDE.md

Guidance for Claude Code working in the `client-desktop` repo.

## What this is

Python desktop relay client. A PyQt6 tray app that connects to the backend
orchestrator over WebSocket and lets a remote peer drive this machine -- mouse,
keyboard, audio, screen, and webcam -- streamed over WebRTC. Linux-oriented:
input injection uses `evdev`. It runs locally on the controlled machine, not in
the Kubernetes cluster.

The relay is the active product: `main.py` wires up only the orchestrator
client plus the mouse/keyboard relay. A separate voice-listener (local
speech-to-text, speaker verification, TTS playback, spectrogram overlay) lives
in the repo under `src/` but is intentionally NOT wired into `main.py` right
now -- it is kept for future use, not deleted. Prioritize the relay path; leave
the voice-listener modules in place, but don't depend on them from `main.py`
unless explicitly asked to re-enable them.

For the whole-system map (orchestrator architecture, the full port map, every
service), see the `jaskier-os/orchestrator` repo and its CLAUDE.md / docs rather
than duplicating it here.

## Layout

- `main.py` -- entrypoint; wires up the tray app.
- `src/orchestrator_client.py` -- WebSocket connection to the orchestrator.
- `src/webrtc_peer.py`, `src/webrtc_audio_track.py` -- WebRTC peer + media.
- `src/mouse_relay.py`, `src/keyboard_relay.py` -- evdev input injection.
- `src/audio_relay.py`, `src/screen_streamer.py`, `src/webcam.py`,
  `src/screenshot.py` -- capture/stream sources.
- `src/config.py` -- env-var config loading.
- `src/tray.py`, `src/settings_dialog.py`, `src/widget.py` -- PyQt6 UI.

Voice-listener modules (present but NOT wired into `main.py`; kept for future
use): `src/transcriber.py`, `src/recorder.py`, `src/speaker_verifier.py`,
`src/audio_monitor.py`, `src/spectrogram.py`, `src/tts_player.py`.

## Build / run

`run.sh` creates a venv, installs `requirements.txt`, and starts the app:

```
./run.sh
```

Manual equivalent: `python3 -m venv venv && venv/bin/pip install -r
requirements.txt && venv/bin/python main.py`. Versions in `requirements.txt` are
pinned for Python 3.12.

## Configuration

All config is env vars; `.env.example` is the source of truth. Copy it to `.env`
(gitignored) and edit. Every setting has a safe plain-connection default; the
only one usually changed is `ORCHESTRATOR_WS_URL` (`ws://` plain, `wss://` for
TLS).

TLS is optional. `TLS_CERT_PATH` is only consulted for `wss://`; leaving it
empty uses the system trust store, and if it points at a missing file the client
falls back safely instead of crashing. Do not make a cert mandatory or assume a
self-signed/VPN cert is always present -- keep the optional, no-crash behaviour.
TURN (`TURN_URL` / `TURN_USERNAME` / `TURN_CREDENTIAL`) is likewise optional;
empty means STUN-only.

## Guardrails

- No emojis anywhere in code, logs, or text.
- No legacy/back-compat shims. After replacing something, remove every orphaned
  field, import, and comment that referenced the old path.
- AI-facing responses/strings are English; only end-user prompts may be Russian.
