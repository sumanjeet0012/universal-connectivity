"""Peer dialling and connection-state operations."""

from __future__ import annotations

import logging

import multiaddr
from libp2p.peer.peerinfo import info_from_p2p_addr

from ..errors import NotReadyError, OperationError, ValidationError

logger = logging.getLogger("app.services.connection")


class ConnectionService:
    """Queue outbound dials and inspect connection state."""

    def __init__(self, engine):
        self._engine = engine

    @property
    def engine(self):
        return self._engine

    @staticmethod
    def validate_multiaddr(value: str) -> str:
        """Validate a dial target and return it unchanged."""
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("'multiaddr' field is required.")
        value = value.strip()
        try:
            info_from_p2p_addr(multiaddr.Multiaddr(value))
        except Exception as exc:
            raise ValidationError(
                "Invalid multiaddr — expected "
                "/ip4/<host>/tcp/<port>/p2p/<peer-id>.",
                detail=str(exc),
            ) from exc
        return value

    def connect(self, maddr: str) -> dict:
        """Queue a dial request. Returns a small acknowledgement payload."""
        target = self.validate_multiaddr(maddr)
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a connection.")
        if not self._engine.connect_to_peer(target):
            raise OperationError("Failed to queue connection request.")
        return {"message": "Connection request queued", "multiaddr": target}

    def is_connected(self, peer_id: str) -> bool:
        try:
            return str(peer_id) in {
                str(peer) for peer in self._engine.host.get_connected_peers()
            }
        except Exception:
            return False
