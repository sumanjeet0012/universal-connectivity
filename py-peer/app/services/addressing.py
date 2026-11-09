"""Listen/announce address resolution and dial-address normalisation.

The engine still owns the libp2p host, but the policy around which addresses
are bindable, which are advertised, and how operator-supplied dial addresses
are rewritten lives here.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
from typing import List

import multiaddr

logger = logging.getLogger("app.services.addressing")

# Transports this build can actually construct. WebSocket is omitted because
# the `websockets` dependency is not installed; WebRTC and WebTransport need
# extra configuration (and `aiortc` for WebRTC). QUIC is intentionally
# excluded: it works experimentally but changes the transport mix, so it is
# rejected explicitly instead of being silently accepted without a registered
# transport.
UNSUPPORTED_LISTEN_PROTOCOLS = (
    "quic",
    "quic-v1",
    "ws",
    "wss",
    "webrtc",
    "webrtc-direct",
    "webtransport",
)


class AddressService:
    """Resolve, validate, rewrite, and snapshot peer addresses."""

    def __init__(self, engine):
        self._engine = engine

    # ------------------------------------------------------------------ #
    #  Listen / announce resolution
    # ------------------------------------------------------------------ #
    def resolve_listen_addrs(self) -> List[multiaddr.Multiaddr]:
        """Parse and validate the configured listen addresses."""
        raw = self._engine.listen_addrs or [
            f"/ip4/0.0.0.0/tcp/{self._engine.port}"
        ]
        resolved = []
        for item in raw:
            try:
                addr = multiaddr.Multiaddr(item)
            except Exception as exc:
                raise ValueError(f"Invalid listen address {item!r}: {exc}") from exc
            protocols = {p.name for p in addr.protocols()}
            unsupported = [
                p for p in UNSUPPORTED_LISTEN_PROTOCOLS if p in protocols
            ]
            if unsupported:
                raise ValueError(
                    f"Unsupported listen address {item!r}: transport "
                    f"{unsupported[0]!r} is not enabled in this build "
                    "(plain TCP only)"
                )
            if "tcp" not in protocols:
                raise ValueError(
                    f"Unsupported listen address {item!r}: expected a TCP multiaddr"
                )
            resolved.append(addr)
        return resolved

    def resolve_announce_addrs(self) -> List[multiaddr.Multiaddr]:
        """Parse explicit announce addresses and reject undialable wildcards."""
        if not self._engine.announce_addrs:
            return []
        resolved = []
        for item in self._engine.announce_addrs:
            try:
                addr = multiaddr.Multiaddr(item)
            except Exception as exc:
                raise ValueError(f"Invalid announce address {item!r}: {exc}") from exc
            protocols = {p.name for p in addr.protocols()}
            if "ip4" in protocols and addr.value_for_protocol("ip4") == "0.0.0.0":
                raise ValueError(
                    f"Announce address {item!r} uses the wildcard host 0.0.0.0; "
                    "use a concrete dialable IP"
                )
            if "ip6" in protocols and addr.value_for_protocol("ip6") in ("::", "::0"):
                raise ValueError(
                    f"Announce address {item!r} uses the wildcard host ::; "
                    "use a concrete dialable IP"
                )
            resolved.append(addr)
        return resolved

    def make_addrs_factory(self):
        """Return an AddrsFactory that never advertises a wildcard address.

        The factory only rewrites wildcard listeners (the socket is bound on
        all interfaces, so a concrete host is valid). A listener explicitly
        bound to a concrete address is advertised as-is; if that address is
        loopback, the caller gets a clear warning rather than a silent rewrite
        that would point at an interface the peer is not listening on.
        """
        if self._engine.announce_addrs:
            return None  # explicit static addresses win
        host = (self._engine.announce_host or "").strip()
        if not host:
            return None
        if host in ("0.0.0.0", "::", "::0"):
            raise ValueError(
                "announce_host cannot be a wildcard address; use a concrete "
                "dialable IP (or an explicit --announce-addr)"
            )
        announce_is_v6 = ":" in host

        def _factory(candidates):
            advertised = []
            seen = set()
            for addr in candidates:
                text = str(addr)
                protocols = {p.name for p in addr.protocols()}
                if "ip4" in protocols:
                    value = addr.value_for_protocol("ip4")
                    if value == "0.0.0.0":
                        if announce_is_v6:
                            continue
                        text = text.replace("/ip4/0.0.0.0/", f"/ip4/{host}/", 1)
                elif "ip6" in protocols:
                    value = addr.value_for_protocol("ip6")
                    if value in ("::", "::0"):
                        if not announce_is_v6:
                            continue
                        text = text.replace("/ip6/::/", f"/ip6/{host}/", 1)
                if text not in seen:
                    seen.add(text)
                    advertised.append(multiaddr.Multiaddr(text))
            return advertised

        return _factory

    # ------------------------------------------------------------------ #
    #  Port probing and listener verification
    # ------------------------------------------------------------------ #
    def warn_if_port_in_use(self, listen_addrs) -> None:
        """Warn when a listen port is already taken by another process.

        py-libp2p enables SO_REUSEPORT (for DCUtR hole punching), so a second
        peer can bind the same port without error. Inbound connections are then
        delivered to whichever process the kernel picks, which surfaces as
        "handshake failed when upgrading to secure connection" because a peer
        with a different PeerID answers. A plain SO_REUSEADDR probe bind still
        fails while a listener is active, so we can detect the situation and
        tell the user to pick a different port.
        """
        probed = set()
        for addr in listen_addrs:
            protocols = {p.name for p in addr.protocols()}
            if "tcp" not in protocols or "ip4" not in protocols:
                continue
            host = addr.value_for_protocol("ip4")
            port = int(addr.value_for_protocol("tcp"))
            bind_host = "0.0.0.0" if host == "0.0.0.0" else host
            if (bind_host, port) in probed:
                continue
            probed.add((bind_host, port))

            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((bind_host, port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    logger.warning(
                        "⚠️  Port %s on %s is already in use by another process. "
                        "libp2p uses SO_REUSEPORT, so this peer will still bind, "
                        "but incoming connections may be handled by the other "
                        "process and fail with 'handshake failed when upgrading "
                        "to secure connection'. Start each local peer with a "
                        "distinct port, e.g. --port 4001 / --port 4002.",
                        port,
                        bind_host,
                    )
                elif exc.errno == errno.EADDRNOTAVAIL:
                    logger.warning(
                        "Listen host %s:%s is not assigned to this machine; the "
                        "listener will fail to start.",
                        bind_host,
                        port,
                    )
                else:
                    logger.warning(
                        "Could not probe listen address %s:%s: %s",
                        bind_host,
                        port,
                        exc,
                    )
            finally:
                probe.close()

    @staticmethod
    def addr_match_key(addr):
        """Normalize a multiaddr for listener comparison.

        A requested port of 0 means "ephemeral"; it is represented as ``None``
        so it matches whatever port the OS assigned.
        """
        protocols = {p.name for p in addr.protocols()}
        family = (
            "ip4" if "ip4" in protocols else "ip6" if "ip6" in protocols else None
        )
        host = addr.value_for_protocol(family) if family else None
        port = None
        for proto in ("tcp", "udp"):
            if proto in protocols:
                port = int(addr.value_for_protocol(proto))
                break
        return (family, host, None if port == 0 else port)

    def verify_listeners(self, host, requested) -> None:
        """Verify that every requested listener actually bound."""
        bound = [str(addr) for addr in host.get_transport_addrs()]
        self._engine.transport_addrs = bound
        if not bound:
            raise RuntimeError("libp2p host did not start any listener")
        bound_keys = [self.addr_match_key(multiaddr.Multiaddr(item)) for item in bound]
        missing = []
        for req in requested:
            family, hostname, port = self.addr_match_key(req)
            matched = any(
                b_family == family
                and b_host == hostname
                and (port is None or b_port == port)
                for b_family, b_host, b_port in bound_keys
            )
            if not matched:
                missing.append(str(req))
        if missing:
            raise RuntimeError(
                "libp2p failed to start listener(s): " + ", ".join(missing)
            )
        logger.info("Listener(s) active: %s", ", ".join(bound))

    # ------------------------------------------------------------------ #
    #  Advertisement
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_wildcard_addr(addr) -> bool:
        """True when a multiaddr binds a wildcard host (0.0.0.0 / ::)."""
        protocols = {p.name for p in addr.protocols()}
        if "ip4" in protocols:
            return addr.value_for_protocol("ip4") == "0.0.0.0"
        if "ip6" in protocols:
            return addr.value_for_protocol("ip6") in ("::", "::0")
        return False

    @staticmethod
    def classify_announced_addr(addr_str: str) -> str:
        """Describe address reachability without claiming it was tested."""
        try:
            addr = multiaddr.Multiaddr(addr_str)
            protocols = {p.name for p in addr.protocols()}
            if "ip4" in protocols:
                ip = ipaddress.ip_address(addr.value_for_protocol("ip4"))
            elif "ip6" in protocols:
                ip = ipaddress.ip_address(addr.value_for_protocol("ip6"))
            else:
                return "non-IP"
        except Exception:
            return "unparsed"
        if ip.is_unspecified:
            return "wildcard (not dialable)"
        if ip.is_loopback:
            return "loopback (same-machine only)"
        if ip.is_link_local:
            return "link-local (same link only)"
        if ip.is_private:
            return "private/LAN (reachable only on that network)"
        return "public/unknown (reachability not verified)"

    def refresh_advertised_addrs(self, host) -> None:
        """Snapshot and log host.get_addrs() after listeners are ready."""
        advertised = [str(addr) for addr in host.get_addrs()]
        self._engine.full_multiaddrs = advertised
        self._engine.full_multiaddr = advertised[0] if advertised else None
        if not advertised:
            logger.warning(
                "No advertised addresses: this peer can dial out, but has no "
                "address to share. Check --listen-addr / --announce-host / "
                "--announce-addr (the address family must match)."
            )
            return
        for addr in advertised:
            category = self.classify_announced_addr(addr)
            logger.info("Advertised address [%s]: %s", category, addr)
            if category.startswith("loopback"):
                logger.warning(
                    "Advertising a loopback address: only peers on this machine "
                    "can dial it. Use --announce-host <LAN IP> or "
                    "--announce-addr <multiaddr> for other peers."
                )

    # ------------------------------------------------------------------ #
    #  Dial addresses
    # ------------------------------------------------------------------ #
    @staticmethod
    def normalize_dial_addr(multiaddr_str: str) -> str:
        """Rewrite wildcard hosts to loopback so pasted addresses are dialable.

        Peers advertise their listen address as /ip4/0.0.0.0/... in older
        versions; 0.0.0.0 only makes sense for binding, never for dialing.
        """
        normalized = multiaddr_str
        if "/ip4/0.0.0.0/" in normalized:
            normalized = normalized.replace("/ip4/0.0.0.0/", "/ip4/127.0.0.1/")
        if "/ip6/::/" in normalized:
            normalized = normalized.replace("/ip6/::/", "/ip6/::1/")
        if normalized != multiaddr_str:
            logger.warning(
                "Dial address contains a wildcard host; using loopback instead: "
                "%s -> %s",
                multiaddr_str,
                normalized,
            )
        return normalized
