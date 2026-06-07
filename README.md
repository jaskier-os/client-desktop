# client-desktop

Desktop relay client for the orchestrator. A lightweight PyQt6 system-tray
application that connects to the orchestrator over a WebSocket and relays remote
desktop control and media:

- Remote mouse control (via `evdev` uinput).
- Remote keyboard input.
- Desktop system-audio streaming to a phone over WebRTC (PulseAudio monitor capture).
- Screen streaming (via `ffmpeg`) and on-demand screenshots.
- On-demand webcam photo capture (via OpenCV).

It runs in the system tray; a Settings dialog shows the active connection and
toggles launch-on-startup.

## Prerequisites

- Linux desktop with a system tray (the app is a tray application).
- Python 3.12 (3.10+ should work).
- System tools used at runtime for media features:
  - `ffmpeg` (screen streaming)
  - PulseAudio / PipeWire with `parec` (desktop audio capture)
  - `uinput` access for the mouse relay (the user must be able to write to
    `/dev/uinput`, e.g. via the `input` group or a udev rule).
- For headless smoke tests, set `QT_QPA_PLATFORM=offscreen`.

## Setup

1. Copy the example environment file and edit it:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env`. Every variable has a safe default; in most cases you only set
   `ORCHESTRATOR_WS_URL`:

   | Variable              | Description                                                                 |
   |-----------------------|-----------------------------------------------------------------------------|
   | `ORCHESTRATOR_WS_URL` | Orchestrator WebSocket endpoint. `ws://` plain, `wss://` for TLS.           |
   | `DEVICE_ID`           | Logical device id reported to the orchestrator.                             |
   | `ORCHESTRATOR_MODEL`  | Default model to route requests to (`sonnet`, `opus`, `haiku`).             |
   | `TLS_CERT_PATH`       | Optional CA/PEM cert for `wss://`. See TLS section below.                   |
   | `TURN_URL`            | Optional WebRTC TURN relay. STUN-only if empty.                             |
   | `TURN_USERNAME`       | Optional TURN username.                                                     |
   | `TURN_CREDENTIAL`     | Optional TURN credential.                                                   |
   | `STUN_URL`            | Public STUN server (has a sensible default).                                |

## Build

There is nothing to compile. Install the pinned Python dependencies:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

## Run

```bash
bash run.sh
```

`run.sh` creates the venv, installs dependencies, reminds you to create `.env`,
and launches the tray app. Or run directly:

```bash
venv/bin/python main.py            # add --log-level DEBUG for verbose logs
```

## TLS / VPN / certificates (optional)

This client requires **no VPN and no certificate** to run. By default it makes a
plain WebSocket connection to `ORCHESTRATOR_WS_URL`.

- With a `ws://` URL: plain connection, no TLS.
- With a `wss://` URL and **no** `TLS_CERT_PATH`: TLS using the system trust
  store (works for publicly trusted certificates).
- With a `wss://` URL and `TLS_CERT_PATH` pointing to an existing CA/PEM file:
  TLS validated against that certificate (use this for a self-signed or
  private/VPN CA).

If `TLS_CERT_PATH` is unset or the file is missing, the client falls back to a
plain/system-trust connection instead of crashing. Certificate and key files are
never committed (see `.gitignore`).

WebRTC media streaming uses a public STUN server by default. Provide a `TURN_URL`
(and optional credentials) only if direct/NAT connectivity is insufficient.

## Models / weights

This relay client uses **no machine-learning model weights** and downloads
nothing at startup. The `models/` directory is gitignored as a precaution; it is
not required for the client to run.

## Notes

- Configuration is environment-driven (`.env` loaded via `python-dotenv`); there
  is no committed config file containing secrets or hostnames.
- No hardcoded IP addresses: the orchestrator endpoint and TURN server are
  configured entirely through environment variables.
