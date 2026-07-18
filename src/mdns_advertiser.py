"""Advertise the LAN-direct audio-relay server over mDNS.

The phone browses for `_repository-relay._tcp` on the local network; when it finds
this service it connects its audio-relay signaling straight to the advertised
host:port instead of the cloud orchestrator. Advertising is best-effort: any
failure (no network, mDNS blocked) is logged and ignored so the desktop keeps
working through the cloud path.
"""

import logging
import socket

log = logging.getLogger(__name__)

# The service label (between the leading underscore and ._tcp) must be <= 15
# bytes per RFC 6763, so this is abbreviated rather than "repository-relay".
SERVICE_TYPE = "_repo-relay._tcp.local."


class MdnsAdvertiser:
    """Registers the local relay server as an mDNS service."""

    def __init__(self, port, device_id="desktop-listener"):
        self._port = port
        self._device_id = device_id
        self._zc = None
        self._info = None

    def start(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except Exception as e:
            log.warning("mDNS advertise unavailable (zeroconf not installed): %s", e)
            return
        try:
            addrs = self._local_addresses()
            if not addrs:
                log.warning("mDNS advertise: no non-loopback IPv4 address found")
                return
            name = f"{self._device_id}.{SERVICE_TYPE}"
            self._info = ServiceInfo(
                SERVICE_TYPE,
                name,
                addresses=addrs,
                port=self._port,
                properties={"deviceId": self._device_id, "path": "/ws/device"},
                server=f"{socket.gethostname()}.local.",
            )
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            log.info("mDNS advertising %s on port %d", name, self._port)
        except Exception as e:
            log.warning("mDNS advertise failed: %s", e)
            self._cleanup()

    # Interface name prefixes that are never the phone's LAN path: container
    # bridges, virtual ethernet, VPN tunnels, WiFi-Direct, Hamachi/ZeroTier.
    _VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "tun", "tap", "p2p", "zt", "wg", "ham")

    @staticmethod
    def _is_private_lan(ip):
        """True for RFC1918 private addresses -- the only ones a same-LAN phone
        can route to. Excludes public ranges some VPNs (e.g. Hamachi's 25.x)
        squat on, which would make the phone try an unreachable address."""
        if ip.startswith("192.168.") or ip.startswith("10."):
            return True
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
            except (IndexError, ValueError):
                return False
            return 16 <= second <= 31
        return False

    def _local_addresses(self):
        """Return packed IPv4 addresses for real (physical/WiFi) LAN interfaces.

        Only RFC1918 private addresses on non-virtual interfaces are advertised,
        so the phone always connects to a routable same-LAN address rather than a
        container bridge, VPN tunnel, or a public range hijacked by a VPN.
        """
        packed = []
        try:
            import ifaddr

            for adapter in ifaddr.get_adapters():
                iface = (adapter.name or "")
                iface_l = iface.lower() if isinstance(iface, str) else iface.decode(errors="ignore").lower()
                if iface_l.startswith(self._VIRTUAL_IFACE_PREFIXES):
                    continue
                for ip in adapter.ips:
                    if not isinstance(ip.ip, str):
                        continue  # IPv6 tuples -- skip, phone LAN path is IPv4
                    if not self._is_private_lan(ip.ip):
                        continue
                    packed.append(socket.inet_aton(ip.ip))
        except Exception as e:
            log.warning("mDNS advertise: address enumeration failed: %s", e)
        return packed

    def stop(self):
        self._cleanup()

    def _cleanup(self):
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception:
                pass
            self._zc = None
            self._info = None
