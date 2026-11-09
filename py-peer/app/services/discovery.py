"""Topic, PubSub, and DHT discovery operations.

Subscription requests arrive from adapters on non-trio threads, so they are
validated here and queued for the trio worker that owns the chat room. The
read-side methods snapshot transport state for adapters without exposing
libp2p objects.
"""

from __future__ import annotations

import logging
import time
from queue import Empty, Full

import trio

from core.chatroom import MAX_TOPIC_NAME_LENGTH, MAX_TOPICS, PUBSUB_DISCOVERY_TOPIC

from ..constants import BOOTSTRAP_PEERS
from ..errors import (
    ConflictError,
    NotReadyError,
    NotFoundError,
    OperationError,
    ValidationError,
)

logger = logging.getLogger("app.services.discovery")


class DiscoveryService:
    """Topic subscription and network-topology queries."""

    def __init__(
        self,
        engine,
        storage=None,
        messaging=None,
        notifications=None,
        connections=None,
    ):
        self._engine = engine
        self._storage = storage
        self._messaging = messaging
        self._notifications = notifications
        self._connections = connections

    @property
    def engine(self):
        return self._engine

    # ------------------------------------------------------------------ #
    #  Topic views
    # ------------------------------------------------------------------ #
    def _subscribed_topics(self) -> set[str]:
        if self._messaging is not None:
            return self._messaging.subscribed_topics()
        chat_room = getattr(self._engine, "chat_room", None)
        return set(chat_room.get_subscribed_topics()) if chat_room else set()

    def topics(self) -> dict:
        if self._storage is not None:
            return self._storage.topics()
        return self._engine.get_all_topics_with_info()

    def topic_info(self, topic: str) -> dict:
        topics = self.topics()
        if topic not in topics:
            raise NotFoundError(f"Topic '{topic}' not found or not subscribed.")
        return {"topic": topic, **topics[topic]}

    def subscribe(self, topic: str) -> dict:
        """Queue a subscription request after enforcing app-level limits."""
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a subscription.")
        if not isinstance(topic, str) or not topic.strip():
            raise ValidationError("'topic' field is required.")
        topic = topic.strip()
        if topic == PUBSUB_DISCOVERY_TOPIC:
            raise ValidationError("That topic name is reserved for peer discovery.")
        already = self._subscribed_topics()
        if topic in already:
            raise ConflictError(f"Already subscribed to topic '{topic}'.")
        if len(already) >= MAX_TOPICS:
            raise ConflictError(f"Topic limit reached ({MAX_TOPICS}).")
        if not self.queue_subscription(topic):
            raise OperationError("Failed to queue subscription.")
        return {
            "message": f"Subscription request queued for '{topic}'",
            "topic": topic,
        }

    def queue_subscription(self, topic_name: str) -> bool:
        """Validate and queue a topic subscription (thread-safe)."""
        engine = self._engine
        chat_room = getattr(engine, "chat_room", None)
        if not chat_room or not getattr(engine, "running", False):
            logger.warning(
                "Cannot subscribe to topic: chat room not ready or service not running"
            )
            return False
        if not isinstance(topic_name, str) or not topic_name:
            logger.warning("Cannot subscribe to an empty/non-string topic")
            return False
        if len(topic_name) > MAX_TOPIC_NAME_LENGTH:
            logger.warning(
                f"Cannot subscribe to oversized topic "
                f"({len(topic_name)} > {MAX_TOPIC_NAME_LENGTH} chars)"
            )
            return False
        if topic_name == PUBSUB_DISCOVERY_TOPIC:
            logger.warning(f"Cannot subscribe to reserved topic: {topic_name}")
            return False
        if len(self._subscribed_topics()) >= MAX_TOPICS:
            logger.warning(f"Cannot subscribe to topic: limit {MAX_TOPICS} reached")
            return False

        queue = getattr(engine, "topic_subscription_queue", None)
        if queue is None:
            logger.warning("Topic subscription queue is not ready")
            return False
        try:
            queue.sync_q.put_nowait(
                {"topic": topic_name, "timestamp": time.time()}
            )
            logger.info(f"Queued subscription request for topic: {topic_name}")
            return True
        except Full:
            logger.warning("Topic subscription queue is full; dropping request")
            return False
        except Exception as e:
            logger.error(f"Failed to queue topic subscription: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Workers
    # ------------------------------------------------------------------ #
    async def process_topic_subscriptions(self) -> None:
        """Process topic subscription requests from adapters."""
        while getattr(self._engine, "running", False):
            queue = getattr(self._engine, "topic_subscription_queue", None)
            if queue is None:
                await trio.sleep(0.1)
                continue
            try:
                try:
                    subscription_data = queue.sync_q.get_nowait()
                    if subscription_data and "topic" in subscription_data:
                        topic_name = subscription_data["topic"]

                        if self._engine.chat_room and self._engine.running:
                            success = await self._engine.chat_room.subscribe_to_topic(
                                topic_name
                            )
                            if success:
                                logger.info(
                                    f"Successfully subscribed to topic: {topic_name}"
                                )
                                if self._notifications is not None:
                                    self._notifications.publish_event(
                                        "topic_subscribed", {"topic": topic_name}
                                    )
                                    await self._notifications.system(
                                        f"Subscribed to topic: {topic_name}"
                                    )
                            else:
                                logger.warning(
                                    f"Failed to subscribe to topic: {topic_name}"
                                )
                                await self._system(
                                    f"Failed to subscribe to topic: {topic_name}"
                                )
                        else:
                            logger.warning(
                                "Cannot subscribe to topic: chat room not ready"
                            )
                            await self._system(
                                "Cannot subscribe to topic: chat room not ready"
                            )

                except Empty:
                    await trio.sleep(0.1)
                except Exception as e:
                    logger.error(f"Error processing topic subscription: {e}")
                    await trio.sleep(0.1)

            except Exception as e:
                logger.error(f"Error in topic subscription processing: {e}")
                await trio.sleep(0.1)

    # ------------------------------------------------------------------ #
    #  PubSub snapshots
    # ------------------------------------------------------------------ #
    def mesh(self) -> dict:
        """Snapshot GossipSub mesh, retrying on concurrent modification."""
        gossipsub = getattr(self._engine, "gossipsub", None)
        if not gossipsub:
            return {}
        mesh = getattr(gossipsub, "mesh", {})
        for _ in range(3):
            try:
                return {
                    topic: [str(p) for p in peers]
                    for topic, peers in mesh.items()
                }
            except RuntimeError:
                continue
        return {}

    def mesh_peers(self, topic: str) -> list[str]:
        return list(self.mesh().get(topic, []))

    def fanout(self) -> dict:
        """Snapshot GossipSub fanout, retrying on concurrent modification."""
        gossipsub = getattr(self._engine, "gossipsub", None)
        if not gossipsub:
            return {}
        fanout = getattr(gossipsub, "fanout", {})
        for _ in range(3):
            try:
                return {
                    topic: [str(p) for p in peers]
                    for topic, peers in fanout.items()
                }
            except RuntimeError:
                continue
        return {}

    def pubsub_peers(self) -> list[str]:
        """Snapshot pubsub peers, retrying if the live dict changes mid-copy."""
        pubsub = getattr(self._engine, "pubsub", None)
        if not pubsub:
            return []
        for _ in range(3):
            try:
                return [str(p) for p in pubsub.peers.keys()]
            except RuntimeError:
                continue
        return []

    def subscriptions(self) -> list[str]:
        if self._messaging is not None:
            return sorted(self._messaging.all_pubsub_topics())
        chat_room = getattr(self._engine, "chat_room", None)
        if chat_room is None:
            return []
        return sorted(chat_room.get_all_subscriptions())

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
        """Re-trigger bootstrap dials. Returns queued/total counts."""
        queued = 0
        for addr in BOOTSTRAP_PEERS:
            try:
                if self._connections is not None:
                    accepted = self._connections.queue_connection(addr)
                else:
                    accepted = self._engine.connect_to_peer(addr)
                if accepted:
                    queued += 1
            except Exception:
                logger.debug(
                    "Bootstrap dial queueing failed for %s", addr, exc_info=True
                )
        return {
            "message": f"Queued connections to {queued} bootstrap peers.",
            "bootstrap_peers_count": queued,
        }

    async def _system(self, message: str) -> None:
        if self._notifications is not None:
            await self._notifications.system(message)
