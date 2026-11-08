"""Stored-message queries over the runtime's storage backend."""

from __future__ import annotations

from ..errors import ValidationError
from ..storage import MAX_HISTORY_LIMIT


class StorageService:
    """Read-side view of retained application data.

    The repository owns identity, dedupe, and retention; this service validates
    caller input and turns pages into the API-facing envelope. ``engine`` is
    still used for subscribed-topic metadata so the store never has to know
    about pubsub state.
    """

    def __init__(self, engine, store=None):
        self._engine = engine
        self._store = store or getattr(engine, "message_store", None)
        if self._store is None:
            raise ValueError(
                "StorageService requires a message store "
                "(pass store= or set engine.message_store)"
            )

    @property
    def engine(self):
        return self._engine

    @property
    def store(self):
        return self._store

    @property
    def backend(self) -> str:
        return self._store.backend

    @property
    def durable(self) -> bool:
        return bool(getattr(self._store, "durable", False))

    def history(self, topic: str, limit: int = 100, offset: int = 0) -> dict:
        """Return a paginated slice of retained topic history."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValidationError(
                f"'limit' must be between 1 and {MAX_HISTORY_LIMIT}."
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValidationError("'offset' must be zero or greater.")
        messages, total = self._store.page(topic, limit=limit, offset=offset)
        return {
            "topic": topic,
            "messages": messages,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def unread(self, topic: str) -> int:
        return self._store.unread_count(topic)

    def mark_read(self, topic: str) -> None:
        self._store.mark_read(topic)

    def topics(self) -> dict:
        return self._engine.get_all_topics_with_info()

    def health(self) -> dict:
        return self._store.health()
