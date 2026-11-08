"""Peer identity and connection-state queries."""

from __future__ import annotations

from typing import Any

from libp2p.peer.id import ID

from ..errors import NotFoundError, ValidationError
from ..models import ConnectionSummary, PeerIdentity


class PeerService:
    """Local identity plus read-only peer state.

    Wraps the shared engine; contains no HTTP/UI framework code.
    """

    def __init__(self, engine):
        self._engine = engine

    @property
    def engine(self):
        return self._engine

    def identity(self) -> PeerIdentity:
        return PeerIdentity(
            peer_id=str(self._engine.host.get_id()),
            nickname=self._engine.nickname,
        )

    def connected_peers(self) -> list[str]:
        info = self._engine.get_connection_info()
        return sorted(str(peer) for peer in info.get("connected_peers", set()))

    def peer_count(self) -> int:
        return self._engine.get_connection_info().get("peer_count", 0)

    def connection_info(self) -> ConnectionSummary:
        info = self._engine.get_connection_info()
        return ConnectionSummary(
            peer_id=info.get("peer_id", ""),
            nickname=info.get("nickname", self._engine.nickname),
            multiaddr=info.get("multiaddr"),
            multiaddrs=list(info.get("multiaddrs") or []),
            listen_addrs=list(info.get("listen_addrs") or []),
            connected_peers=sorted(
                str(peer) for peer in info.get("connected_peers", set())
            ),
        )

    def listening_addrs(self) -> list[str]:
        return list(self._engine.transport_addrs)

    def advertised_addrs(self) -> list[str]:
        return list(self._engine.full_multiaddrs)

    def known_peers(self) -> list[str]:
        try:
            peerstore = self._engine.host.get_peerstore()
            return sorted(str(peer) for peer in peerstore.peers_with_addrs())
        except Exception as exc:  # pragma: no cover - library failure path
            raise ValidationError(f"Could not read peerstore: {exc}") from exc

    def peer_detail(self, peer_id: str) -> dict[str, Any]:
        """Return peerstore addresses for a peer ID."""
        try:
            pid = ID.from_base58(peer_id)
        except Exception as exc:
            raise ValidationError("Invalid peer ID.") from exc
        try:
            pinfo = self._engine.host.get_peerstore().peer_info(pid)
        except Exception as exc:
            raise NotFoundError(f"Peer not found: {exc}") from exc
        return {
            "peer_id": peer_id,
            "addrs": [str(addr) for addr in (pinfo.addrs or [])],
        }

    def is_connected(self, peer_id: str) -> bool:
        return peer_id in self.connected_peers()
