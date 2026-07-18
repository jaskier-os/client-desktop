"""Environment-driven configuration for the desktop relay client.

All environment-specific settings (orchestrator endpoint, TURN server, optional
TLS CA certificate) are read from environment variables. A local `.env` file is
loaded automatically if present (see `.env.example`).

Defaults are plain/localhost so the client runs with no VPN, no certificates,
and no external infrastructure assumptions.
"""

import os

try:
    # Optional: load a local .env file if python-dotenv is installed.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # python-dotenv is optional; environment variables still work without it.
    pass


def get(name, default=None):
    """Return an environment variable value, falling back to default."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


# Orchestrator WebSocket endpoint. Plain ws:// by default; use wss:// for TLS.
ORCHESTRATOR_WS_URL = get("ORCHESTRATOR_WS_URL", "ws://localhost:10001/ws/device")

# Logical device identifier reported to the orchestrator.
DEVICE_ID = get("DEVICE_ID", "desktop-listener")

# Default model the orchestrator should route requests to.
ORCHESTRATOR_MODEL = get("ORCHESTRATOR_MODEL", "sonnet")

# Optional path to a CA/PEM certificate used ONLY for wss:// connections.
# If unset or the file does not exist, the client falls back to the system
# trust store (for public certs) or a plain connection (for ws://). Never
# required, never crashes when missing.
TLS_CERT_PATH = get("TLS_CERT_PATH")

# Optional TURN server for WebRTC media relay. If unset, WebRTC uses STUN only
# (public Google STUN), which works for most direct/NAT scenarios. Provide a
# TURN server when peers cannot reach each other directly.
TURN_URL = get("TURN_URL")                 # e.g. "turn:turn.example.com:3478"
TURN_USERNAME = get("TURN_USERNAME")
TURN_CREDENTIAL = get("TURN_CREDENTIAL")

# Optional override for the public STUN server.
STUN_URL = get("STUN_URL", "stun:stun.l.google.com:19302")

# LAN-direct audio relay. When the phone is on the same network it can connect
# its audio-relay signaling straight to this desktop (skipping the cloud hop).
# The desktop runs a small local WebSocket server on this port and advertises it
# over mDNS. Set LOCAL_RELAY_ENABLED=false to disable both.
LOCAL_RELAY_ENABLED = get("LOCAL_RELAY_ENABLED", "true").lower() not in ("0", "false", "no")
LOCAL_RELAY_PORT = int(get("LOCAL_RELAY_PORT", "10101"))


def resolve_tls_cert():
    """Return an existing TLS cert path, or None if unset/missing.

    Missing certificates must never crash the client; we simply return None and
    the caller falls back to a plain or system-trust connection.
    """
    if TLS_CERT_PATH and os.path.isfile(TLS_CERT_PATH):
        return TLS_CERT_PATH
    return None
