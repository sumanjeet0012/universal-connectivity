"""
Topic / channel endpoints.

GET    /api/v1/topics              - list all subscribed topics with metadata
POST   /api/v1/topics              - subscribe to a new topic
GET    /api/v1/topics/{topic}/info - topic details (unread, total, last msg)
GET    /api/v1/topics/{topic}/peers - peers in GossipSub mesh for topic
"""

from .base import BaseHandler, parse_string_field
from chatroom import PUBSUB_DISCOVERY_TOPIC
from headless import MAX_TOPIC_NAME_LENGTH, MAX_TOPICS


class TopicListHandler(BaseHandler):
    """GET /api/v1/topics — POST /api/v1/topics"""

    def get(self):
        if not self.require_ready():
            return
        topics_info = self.service.get_all_topics_with_info()
        # Ensure last_message timestamps are serialisable
        for v in topics_info.values():
            if v.get("last_message") and not isinstance(v["last_message"], dict):
                v["last_message"] = None
        self.send_success({"topics": topics_info, "count": len(topics_info)})

    def post(self):
        if not self.require_ready():
            return
        body = self.get_json_body()
        try:
            topic = parse_string_field(
                body,
                "topic",
                required=True,
                max_length=MAX_TOPIC_NAME_LENGTH,
            )
        except ValueError as e:
            self.send_error_response(str(e))
            return

        if topic == PUBSUB_DISCOVERY_TOPIC:
            self.send_error_response(
                "That topic name is reserved for peer discovery.", status=400
            )
            return

        already = self.service.get_subscribed_topics()
        if topic in already:
            self.send_error_response(f"Already subscribed to topic '{topic}'.", status=409)
            return
        if len(already) >= MAX_TOPICS:
            self.send_error_response(
                f"Topic limit reached ({MAX_TOPICS}).", status=409
            )
            return

        queued = self.service.subscribe_to_topic(topic)
        if queued:
            self.send_success({"message": f"Subscription request queued for '{topic}'", "topic": topic}, status=202)
        else:
            self.send_error_response("Failed to queue subscription — service not ready.", status=503)


class TopicInfoHandler(BaseHandler):
    """GET /api/v1/topics/{topic}/info"""

    def get(self, topic):
        if not self.require_ready():
            return
        all_info = self.service.get_all_topics_with_info()
        if topic not in all_info:
            self.send_error_response(f"Topic '{topic}' not found or not subscribed.", status=404)
            return
        info = all_info[topic]
        if info.get("last_message") and not isinstance(info["last_message"], dict):
            info["last_message"] = None
        self.send_success({"topic": topic, **info})


class TopicMeshPeersHandler(BaseHandler):
    """GET /api/v1/topics/{topic}/peers — peers in GossipSub mesh for topic"""

    def get(self, topic):
        if not self.require_ready():
            return
        mesh = self.service.get_gossipsub_mesh()
        peers_in_mesh = mesh.get(topic, [])
        self.send_success({
            "topic": topic,
            "mesh_peers": peers_in_mesh,
            "count": len(peers_in_mesh),
        })
