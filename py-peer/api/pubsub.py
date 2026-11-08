"""
PubSub / GossipSub endpoints.

GET /api/v1/pubsub/peers         - all peers connected via PubSub
GET /api/v1/pubsub/mesh          - GossipSub mesh per topic
GET /api/v1/pubsub/fanout        - GossipSub fanout per topic
GET /api/v1/pubsub/config        - GossipSub configuration values
GET /api/v1/pubsub/subscriptions - active subscription topic names
"""

from .base import BaseHandler


class PubSubPeersHandler(BaseHandler):
    """GET /api/v1/pubsub/peers"""

    def get(self):
        if not self.require_ready():
            return
        peers = self.service.get_pubsub_peers()
        self.send_success({"peers": peers, "count": len(peers)})


class PubSubMeshHandler(BaseHandler):
    """GET /api/v1/pubsub/mesh"""

    def get(self):
        if not self.require_ready():
            return
        mesh = self.service.get_gossipsub_mesh()
        total_peers = sum(len(v) for v in mesh.values())
        self.send_success({
            "mesh": mesh,
            "topic_count": len(mesh),
            "total_mesh_peers": total_peers,
        })


class PubSubFanoutHandler(BaseHandler):
    """GET /api/v1/pubsub/fanout"""

    def get(self):
        if not self.require_ready():
            return
        fanout = self.service.get_gossipsub_fanout()
        self.send_success({"fanout": fanout})


class PubSubConfigHandler(BaseHandler):
    """GET /api/v1/pubsub/config"""

    def get(self):
        if not self.require_ready():
            return
        gs = self.service.gossipsub
        self.send_success({
            "degree": getattr(gs, "degree", None),
            "degree_low": getattr(gs, "degree_low", None),
            "degree_high": getattr(gs, "degree_high", None),
            "gossip_window": getattr(gs, "gossip_window", None),
            "gossip_history": getattr(gs, "gossip_history", None),
            "heartbeat_interval": getattr(gs, "heartbeat_interval", None),
            "heartbeat_initial_delay": getattr(gs, "heartbeat_initial_delay", None),
            "protocols": [str(p) for p in getattr(gs, "protocols", [])],
        })


class PubSubSubscriptionsHandler(BaseHandler):
    """GET /api/v1/pubsub/subscriptions"""

    def get(self):
        if not self.require_ready():
            return
        topics = sorted(self.service.get_all_pubsub_topics())
        self.send_success({"subscriptions": topics, "count": len(topics)})
