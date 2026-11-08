"""Tests for durable message storage: identity, dedupe, retention, migrations."""

import asyncio
import os
import queue
import shutil
import sqlite3
import tempfile
import unittest

from app.errors import StorageError, ValidationError
from app.services import StorageService
from app.storage import (
    SCHEMA_VERSION,
    MemoryStore,
    SqliteStore,
    message_identity,
    open_store,
)
from chatroom import ChatMessage
from headless import HeadlessService


class _FakeJanusQueue:
    def __init__(self, maxsize=0):
        self.sync_q = queue.Queue(maxsize=maxsize)


def _message(text, *, topic="t", sender="peer-a", timestamp=1.0, read=False, **extra):
    payload = {
        "message": text,
        "topic": topic,
        "sender_id": sender,
        "sender_nick": "A",
        "timestamp": timestamp,
        "read": read,
    }
    payload.update(extra)
    return payload


class MessageIdentityTests(unittest.TestCase):
    def test_same_frame_has_the_same_identity(self):
        first = _message("hi", timestamp=10.0)
        second = _message("hi", timestamp=10.0)
        self.assertEqual(message_identity(first), message_identity(second))

    def test_content_timestamp_and_topic_change_identity(self):
        base = _message("hi", timestamp=10.0)
        variants = [
            _message("other", timestamp=10.0),
            _message("hi", timestamp=11.0),
            _message("hi", topic="other", timestamp=10.0),
        ]
        identities = {message_identity(base)} | {
            message_identity(variant) for variant in variants
        }
        self.assertEqual(len(identities), 4)

    def test_explicit_message_id_wins(self):
        self.assertEqual(message_identity({"message_id": "abc"}), "abc")


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()

    def test_append_and_paginate_oldest_first(self):
        for index in range(5):
            self.assertTrue(self.store.messages.append(_message(f"m{index}", timestamp=index)))
        page, total = self.store.page("t", limit=2, offset=1)
        self.assertEqual(total, 5)
        self.assertEqual([m["message"] for m in page], ["m1", "m2"])
        self.assertEqual(page[0]["message_id"], message_identity(_message("m1", timestamp=1)))

    def test_duplicate_is_rejected_and_counted_once(self):
        self.assertTrue(self.store.messages.append(_message("hi", timestamp=10.0)))
        self.assertFalse(self.store.messages.append(_message("hi", timestamp=10.0)))
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.unread_count("t"), 1)

    def test_retention_trims_oldest_and_adjusts_unread(self):
        for index in range(5):
            self.store.messages.append(_message(f"m{index}", timestamp=index), keep=3)
        page, total = self.store.page("t")
        self.assertEqual(total, 3)
        self.assertEqual([m["message"] for m in page], ["m2", "m3", "m4"])
        self.assertEqual(self.store.unread_count("t"), 3)
        self.assertEqual(self.store.stats("t")["total_count"], 3)

    def test_own_messages_are_stored_read(self):
        self.store.messages.append(_message("mine", read=True))
        self.assertEqual(self.store.messages.unread_count("t"), 0)

    def test_mark_read_returns_and_persists(self):
        self.store.messages.append(_message("a"))
        self.store.messages.append(_message("b"))
        self.assertEqual(self.store.messages.mark_read("t"), 2)
        self.assertEqual(self.store.messages.unread_count("t"), 0)
        page, _total = self.store.page("t")
        self.assertTrue(all(message["read"] for message in page))

    def test_history_rows_are_copies(self):
        self.store.messages.append(_message("a"))
        page, _total = self.store.page("t")
        page[0]["message"] = "mutated"
        page[0]["read"] = True
        fresh, _total = self.store.page("t")
        self.assertEqual(fresh[0]["message"], "a")
        self.assertFalse(fresh[0]["read"])

    def test_stats_last_message_is_a_copy(self):
        self.store.messages.append(_message("only"))
        stats = self.store.messages.stats("t")
        self.assertEqual(stats["last_message"]["message"], "only")
        stats["last_message"]["message"] = "mutated"
        self.assertEqual(self.store.messages.stats("t")["last_message"]["message"], "only")

    def test_invalid_page_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.page("t", limit=0)
        with self.assertRaises(ValidationError):
            self.store.page("t", offset=-1)

    def test_health_reports_memory_backend(self):
        health = self.store.health()
        self.assertEqual(health["backend"], "memory")
        self.assertFalse(health["durable"])


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "history.db")
        self.store = SqliteStore(self.path)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_schema_version_is_recorded(self):
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, SCHEMA_VERSION)

    def test_messages_survive_a_restart(self):
        self.store.messages.append(_message("before", timestamp=5.0))
        self.store.messages.mark_read("t")
        self.store.close()

        reopened = SqliteStore(self.path)
        try:
            page, total = reopened.page("t")
            self.assertEqual(total, 1)
            self.assertEqual(page[0]["message"], "before")
            self.assertTrue(page[0]["read"])
            self.assertEqual(reopened.unread_count("t"), 0)
        finally:
            reopened.close()

    def test_duplicate_after_restart_is_rejected(self):
        self.store.messages.append(_message("once", timestamp=7.0))
        self.store.close()

        reopened = SqliteStore(self.path)
        try:
            self.assertFalse(reopened.append(_message("once", timestamp=7.0)))
            self.assertEqual(reopened.count(), 1)
        finally:
            reopened.close()

    def test_retention_is_enforced_in_the_database(self):
        for index in range(6):
            self.store.messages.append(_message(f"m{index}", timestamp=index), keep=4)
        page, total = self.store.page("t")
        self.assertEqual(total, 4)
        self.assertEqual([m["message"] for m in page], ["m2", "m3", "m4", "m5"])
        self.assertEqual(self.store.unread_count("t"), 4)

    def test_settings_round_trip_persists(self):
        self.store.settings.set("theme", "dark")
        self.store.settings.set("theme", "light")
        self.store.close()

        reopened = SqliteStore(self.path)
        try:
            self.assertEqual(reopened.settings.get("theme"), "light")
            self.assertEqual(reopened.settings.all(), {"theme": "light"})
            self.assertTrue(reopened.settings.delete("theme"))
            self.assertIsNone(reopened.settings.get("theme"))
        finally:
            reopened.close()

    def test_peer_repository_round_trip(self):
        self.store.peers.upsert(
            "peer-a",
            public_key_hex="aabb",
            protocols=["/chat/1.0.0"],
            listen_addrs=["/ip4/127.0.0.1/tcp/4001"],
            agent_version="py-peer/1",
            updated_at=123.0,
        )
        self.store.close()

        reopened = SqliteStore(self.path)
        try:
            record = reopened.peers.get("peer-a")
            self.assertEqual(record["public_key_hex"], "aabb")
            self.assertEqual(record["protocols"], ["/chat/1.0.0"])
            self.assertEqual([r["peer_id"] for r in reopened.peers.list()], ["peer-a"])
            self.assertTrue(reopened.peers.delete("peer-a"))
            self.assertEqual(reopened.peers.count(), 0)
        finally:
            reopened.close()

    def test_newer_schema_is_refused_instead_of_corrupted(self):
        other = os.path.join(self.directory, "future.db")
        connection = sqlite3.connect(other)
        try:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            SqliteStore(other)

    def test_health_reports_durable_backend(self):
        self.store.messages.append(_message("one"))
        health = self.store.health()
        self.assertEqual(health["backend"], "sqlite")
        self.assertTrue(health["durable"])
        self.assertEqual(health["schema_version"], SCHEMA_VERSION)
        self.assertEqual(health["messages"], 1)
        self.assertEqual(health["peer_records"], 0)
        self.assertEqual(health["settings"], 0)

    def test_closed_store_health_does_not_raise(self):
        self.store.close()
        self.assertTrue(self.store.health()["closed"])
        self.store.close()  # idempotent


class OpenStoreTests(unittest.TestCase):
    def test_no_path_returns_the_in_memory_store(self):
        store = open_store(None)
        self.assertIsInstance(store, MemoryStore)
        store.close()

    def test_path_returns_the_sqlite_store(self):
        directory = tempfile.mkdtemp()
        try:
            store = open_store(os.path.join(directory, "nested", "history.db"))
            self.assertIsInstance(store, SqliteStore)
            self.assertTrue(os.path.exists(os.path.join(directory, "nested")))
            store.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class StorageServiceTests(unittest.TestCase):
    def test_history_and_unread_use_the_repository(self):
        store = MemoryStore()
        for index in range(3):
            store.messages.append(_message(f"m{index}", timestamp=index))
        service = StorageService(object(), store)
        page = service.history("t", limit=1, offset=1)
        self.assertEqual(page["total"], 3)
        self.assertEqual([m["message"] for m in page["messages"]], ["m1"])
        self.assertEqual(service.backend, "memory")
        self.assertFalse(service.durable)


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.service = HeadlessService(
            nickname="test", ui_mode=True, message_store=self.store
        )
        self.service.message_queue = _FakeJanusQueue()
        self.service.system_queue = _FakeJanusQueue()
        self.service.host = None
        self.service.chat_room = None

    def _handle(self, text, timestamp=10.0):
        message = ChatMessage(
            message=text,
            sender_id="peer-x",
            sender_nick="X",
            topic="topic-x",
            timestamp=timestamp,
        )
        asyncio.run(self.service._handle_chat_message(message))

    def test_engine_stores_through_the_shared_repository(self):
        self._handle("hello")
        self.assertIs(self.service.message_store, self.store)
        page, total = self.store.page("topic-x")
        self.assertEqual(total, 1)
        self.assertIn("message_id", page[0])

    def test_redelivered_frame_is_stored_and_fanned_out_once(self):
        self._handle("hello", timestamp=10.0)
        self._handle("hello", timestamp=10.0)
        self.assertEqual(len(self.service.get_topic_messages("topic-x")), 1)
        self.assertEqual(self.service.get_unread_count("topic-x"), 1)
        self.assertEqual(self.service.message_queue.sync_q.qsize(), 1)

    def test_same_text_at_a_different_time_is_a_new_message(self):
        self._handle("hello", timestamp=10.0)
        self._handle("hello", timestamp=11.0)
        self.assertEqual(len(self.service.get_topic_messages("topic-x")), 2)

    def test_topic_info_and_mark_read_read_the_repository(self):
        self._handle("one", timestamp=1.0)
        self._handle("two", timestamp=2.0)
        self.service.chat_room = type(
            "Room", (), {"get_subscribed_topics": lambda _self: {"topic-x"}}
        )()
        info = self.service.get_all_topics_with_info()["topic-x"]
        self.assertEqual(info["total_count"], 2)
        self.assertEqual(info["unread_count"], 2)
        self.assertEqual(info["last_message"]["message"], "two")

        self.service.mark_topic_as_read("topic-x")
        self.assertEqual(self.service.get_unread_count("topic-x"), 0)

    def test_storage_failure_keeps_live_delivery_working(self):
        class _BrokenStore(MemoryStore):
            def append(self, message, keep=None):
                raise StorageError("disk on fire")

        broken = _BrokenStore()
        self.service.message_store = broken
        self._handle("still delivered")
        # The message is not stored, but the UI queue and unread state survive.
        self.assertEqual(self.service.message_queue.sync_q.qsize(), 1)
        self.assertEqual(len(broken.page("topic-x")[0]), 0)


if __name__ == "__main__":
    unittest.main()
