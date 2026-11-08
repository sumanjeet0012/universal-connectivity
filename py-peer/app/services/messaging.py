"""Message publishing and history queries."""

from __future__ import annotations

from ..errors import NotReadyError, OperationError, ValidationError


class MessagingService:
    """Publish chat messages through the engine's topic protocol."""

    def __init__(self, engine, storage):
        self._engine = engine
        self._storage = storage

    @property
    def engine(self):
        return self._engine

    @property
    def storage(self):
        return self._storage

    @staticmethod
    def validate_message(message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ValidationError("'message' field is required.")
        return message.strip()

    def send(self, message: str) -> None:
        """Queue a message for the node's default chat topic."""
        text = self.validate_message(message)
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a message.")
        if not self._engine.send_message(text):
            raise OperationError("Failed to queue message.")

    def send_to_topic(self, topic: str, message: str) -> None:
        """Queue a message for a subscribed topic."""
        text = self.validate_message(message)
        if not getattr(self._engine, "ready", False):
            raise NotReadyError("Service not ready — cannot queue a message.")
        if topic not in self._engine.get_subscribed_topics():
            raise ValidationError(
                f"Not subscribed to topic '{topic}'. Subscribe first via POST "
                "/api/v1/topics."
            )
        if not self._engine.send_message_to_topic(topic, text):
            raise OperationError("Failed to queue message.")

    def history(self, topic: str, limit: int = 100, offset: int = 0) -> dict:
        return self._storage.history(topic, limit=limit, offset=offset)

    def unread_count(self, topic: str) -> int:
        return self._storage.unread(topic)

    def mark_read(self, topic: str) -> None:
        self._storage.mark_read(topic)

    def subscribed_topics(self) -> set[str]:
        return set(self._engine.get_subscribed_topics())
