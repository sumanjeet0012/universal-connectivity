"""In-memory application storage facade.

Phase 2 keeps the existing in-process history. Phase 3 will replace this
implementation with a durable repository behind the same read API; write
ownership moves there as part of that phase.
"""

from __future__ import annotations

from ..errors import ValidationError

MAX_HISTORY_LIMIT = 1000


class StorageService:
    """Read-side view of retained application data."""

    backend = "memory"

    def __init__(self, engine):
        self._engine = engine

    @property
    def engine(self):
        return self._engine

    def history(self, topic: str, limit: int = 100, offset: int = 0) -> dict:
        """Return a paginated slice of in-memory topic history."""
        if not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValidationError(
                f"'limit' must be between 1 and {MAX_HISTORY_LIMIT}."
            )
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("'offset' must be zero or greater.")
        messages = self._engine.get_topic_messages(topic)
        return {
            "topic": topic,
            "messages": messages[offset: offset + limit],
            "total": len(messages),
            "limit": limit,
            "offset": offset,
        }

    def unread(self, topic: str) -> int:
        return self._engine.get_unread_count(topic)

    def mark_read(self, topic: str) -> None:
        self._engine.mark_topic_as_read(topic)

    def topics(self) -> dict:
        return self._engine.get_all_topics_with_info()

    def health(self) -> dict:
        return {"backend": self.backend, "durable": False}
