"""Topic, PubSub, and DHT discovery operations."""

from __future__ import annotations

import logging

from chatroom import MAX_TOPICS, PUBSUB_DISCOVERY_TOPIC

from ..errors import ConflictError, NotReadyError, NotFoundError, OperationError, ValidationError

logger = logging.getLogger("app.services.discovery")


class DiscoveryService:
    """Topic subscription and network-topology queries.

    Keeps transport details (GossipSub/DHT) behind application-level results so
    adapters do not import libp2p types.
    """

    def __init__(self, engine):
        self._engine = engine

    @property
    def engine(self):
        return self._engine

    def topics(self) -> dict:
        return self._engine.get_all_topics_with_info()

    def topic_info(self, topic: str) -> dict:
        topics = self._engine.get_all_topics_with_info()
        if topic not in topics:
            raise NotFoundError(
                f"Topic '{topic}' not found or not subscribed."
            )
        return {"topic": topic, **topics[topic]}

    def subscribe(self, topic: str) -> dict:
        """Queue a subscription request after enforcing app-level limits."""
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a subscription.")
        if not isinstance(topic, str) or not topic.strip():
            raise ValidationError("'topic' field is required.")
        topic = topic.strip()
        if topic == PUBSUB_DISCOVERY_TOPIC:
            raise ValidationError(
                "That topic name is reserved for peer discovery."
            )
        already = self._engine.get_subscribed_topics()
        if topic in already:
            raise ConflictError(f"Already subscribed to topic '{topic}'.")
        if len(already) >= MAX_TOPICS:
            raise ConflictError(f"Topic limit reached ({MAX_TOPICS}).")
        if not self._engine.subscribe_to_topic(topic):
            raise OperationError("Failed to queue subscription.")
        return {"message": f"Subscription request queued for '{topic}'", "topic": topic}

    def mesh(self) -> dict:
        return self._engine.get_gossipsub_mesh()

    def mesh_peers(self, topic: str) -> list[str]:
        return list(self.mesh().get(topic, []))

    def fanout(self) -> dict:
        return self._engine.get_gossipsub_fanout()

    def pubsub_peers(self) -> list[str]:
        return self._engine.get_pubsub_peers()

    def subscriptions(self) -> list[str]:
        return sorted(self._engine.get_all_pubsub_topics())

    def gossipsub_config(self) -> dict:
        gossipsub = self._engine.gossipsub
        return {
            "degree": getattr(gossipsub, "degree", None),
            "degree_low": getattr(gossipsub, "degree_low", None),
            "degree_high": getattr(gossipsub, "degree_high", None),
            "gossip_window": getattr(gossipsub, "gossip_window", None),
            "gossip_history": getattr(gossipsub, "gossip_history", None),
            "heartbeat_interval": getattr(gossipsub, "heartbeat_interval", None),
            "heartbeat_initial_delay": getattr(
                gossipsub, "heartbeat_initial_delay", None
            ),
            "protocols": [str(p) for p in getattr(gossipsub, "protocols", [])],
        }

    # ------------------------------------------------------------------ #
    #  DHT
    # ------------------------------------------------------------------ #
    def _require_dht(self):
        dht = getattr(self._engine, "dht", None)
        if not dht:
            raise NotReadyError("DHT is not initialised.")
        return dht

    def dht_status(self) -> dict:
        dht = self._require_dht()
        try:
            routing_table_size = len(list(dht.routing_table.get_peer_ids()))
        except Exception:
            routing_table_size = -1
        mode_val = getattr(dht, "mode", None)
        mode_str = mode_val.name if hasattr(mode_val, "name") else str(mode_val)
        return {
            "mode": mode_str,
            "random_walk_enabled": getattr(dht, "enable_random_walk", False),
            "routing_table_size": routing_table_size,
        }

    def dht_peers(self) -> list[str]:
        dht = self._require_dht()
        try:
            return [str(peer) for peer in dht.routing_table.get_peer_ids()]
        except Exception as exc:  # pragma: no cover - library failure path
            raise OperationError(f"Could not read routing table: {exc}") from exc

    def bootstrap(self) -> dict:
        """Re-trigger bootstrap dials. Returns accepted/total counts."""
        from headless import BOOTSTRAP_PEERS

        queued = 0
        for addr in BOOTSTRAP_PEERS:
            try:
                if self._engine.connect_to_peer(addr):
                    queued += 1
            except Exception:
                logger.debug("Bootstrap dial queueing failed for %s", addr, exc_info=True)
        return {
            "message": f"Queued connections to {queued} bootstrap peers.",
            "bootstrap_peers_count": queued,
        }
