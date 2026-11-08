"""
Peer management endpoints.

GET    /api/v1/peers                  - list connected peers
GET    /api/v1/peers/count            - count of connected peers
GET    /api/v1/peers/known            - peers in peerstore
POST   /api/v1/peers/connect          - connect via multiaddr
GET    /api/v1/peers/{peer_id}        - info for a specific peer
GET    /api/v1/peers/{peer_id}/identify - identify protocol info (from cache)
"""

import multiaddr
from libp2p.peer.peerinfo import info_from_p2p_addr

from .base import BaseHandler, parse_string_field


class PeerListHandler(BaseHandler):
    """GET /api/v1/peers"""

    def get(self):
        if not self.require_ready():
            return
        info = self.service.get_connection_info()
        peers = list(info.get("connected_peers", set()))
        self.send_success({
            "peers": peers,
            "count": len(peers),
        })


class PeerCountHandler(BaseHandler):
    """GET /api/v1/peers/count"""

    def get(self):
        if not self.require_ready():
            return
        info = self.service.get_connection_info()
        self.send_success({"count": info.get("peer_count", 0)})


class KnownPeersHandler(BaseHandler):
    """GET /api/v1/peers/known"""

    def get(self):
        if not self.require_ready():
            return
        try:
            known = [str(p) for p in self.service.host.get_peerstore().peers_with_addrs()]
        except Exception as e:
            self.send_error_response(f"Could not fetch known peers: {e}", status=500)
            return
        self.send_success({"peers": known, "count": len(known)})


class PeerConnectHandler(BaseHandler):
    """POST /api/v1/peers/connect"""

    def post(self):
        if not self.require_ready():
            return
        body = self.get_json_body()
        try:
            maddr = parse_string_field(body, "multiaddr", required=True)
        except ValueError as e:
            self.send_error_response(str(e))
            return
        # Validate before queueing: otherwise the API reports 202 for a typo and
        # the failure only appears later as a system message.
        try:
            info_from_p2p_addr(multiaddr.Multiaddr(maddr))
        except Exception:
            self.send_error_response(
                "Invalid multiaddr — expected /ip4/<host>/tcp/<port>/p2p/<peer-id>.",
                status=400,
            )
            return
        queued = self.service.connect_to_peer(maddr)
        if queued:
            self.send_success({"message": "Connection request queued", "multiaddr": maddr}, status=202)
        else:
            self.send_error_response("Failed to queue connection request — service not ready.", status=503)


class PeerDetailHandler(BaseHandler):
    """GET /api/v1/peers/{peer_id}"""

    def get(self, peer_id):
        if not self.require_ready():
            return
        from libp2p.peer.id import ID

        try:
            pid = ID.from_base58(peer_id)
        except Exception:
            self.send_error_response("Invalid peer ID.", status=400)
            return

        try:
            pinfo = self.service.host.get_peerstore().peer_info(pid)
            addrs = [str(a) for a in pinfo.addrs] if pinfo.addrs else []
        except Exception as e:
            self.send_error_response(f"Peer not found: {e}", status=404)
            return
        self.send_success({
            "peer_id": peer_id,
            "addrs": addrs,
        })


class PeerIdentifyHandler(BaseHandler):
    """GET /api/v1/peers/{peer_id}/identify — returns cached identify data"""

    def get(self, peer_id):
        if not self.require_ready():
            return
        cached = self.service.peer_info_cache.get(peer_id)
        if not cached:
            self.send_error_response(
                f"No cached identify info for peer {peer_id[:12]}... — "
                "connect to the peer first so identify can be triggered.",
                status=404,
            )
            return
        self.send_success({
            "peer_id": peer_id,
            "protocol_version": cached.get("protocol_version"),
            "agent_version": cached.get("agent_version"),
            "listen_addrs": [str(a) for a in cached.get("listen_addrs", [])],
            "protocols": [str(p) for p in cached.get("protocols", [])],
            "cached_at": cached.get("timestamp"),
        })
