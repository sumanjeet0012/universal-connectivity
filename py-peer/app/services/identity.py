"""Local identity and cached Identify-protocol metadata."""

from __future__ import annotations

from ..errors import NotFoundError, ValidationError
from ..models import PeerIdentity


class IdentityService:
    """Identity queries.

    Private key material is never read or returned here; the engine owns key
    construction. Public keys are returned only as hex strings, matching the
    existing API contract.
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

    def peer_id(self) -> str:
        return str(self._engine.host.get_id())

    @staticmethod
    def _normalize_peer_id(peer_id: str) -> str:
        if not isinstance(peer_id, str) or not peer_id.strip():
            raise ValidationError("Peer ID is required.")
        return peer_id.strip()

    @staticmethod
    def _serialize(peer_id: str, info: dict) -> dict:
        return {
            "peer_id": peer_id,
            "protocol_version": info.get("protocol_version"),
            "agent_version": info.get("agent_version"),
            "listen_addrs": [str(addr) for addr in info.get("listen_addrs", [])],
            "protocols": [str(proto) for proto in info.get("protocols", [])],
            "cached_at": info.get("timestamp"),
        }

    def cache(self) -> dict:
        snapshot = self._engine.get_peer_info_cache_snapshot()
        return {
            peer_id: self._serialize(peer_id, info)
            for peer_id, info in snapshot.items()
        }

    def get(self, peer_id: str) -> dict:
        peer_id = self._normalize_peer_id(peer_id)
        info = self._engine.get_peer_info(peer_id)
        if not info:
            raise NotFoundError(
                f"No cached identify info for peer '{peer_id[:16]}...'. "
                "Connect to the peer first so that the identify protocol is "
                "triggered automatically."
            )
        return self._serialize(peer_id, info)

    def public_key_hex(self, peer_id: str) -> str:
        peer_id = self._normalize_peer_id(peer_id)
        pubkey = self._engine.get_public_key_for_peer(peer_id)
        if pubkey is None:
            raise NotFoundError(
                f"No public key cached for peer '{peer_id[:16]}...'."
            )
        return pubkey.hex() if isinstance(pubkey, bytes) else str(pubkey)

    def invalidate(self, peer_id: str) -> None:
        peer_id = self._normalize_peer_id(peer_id)
        if not self._engine.delete_peer_info(peer_id):
            raise NotFoundError(
                f"No cache entry found for peer '{peer_id[:16]}...'."
            )
