"""Tests for the shared runtime, event bus, and single-instance wiring."""

import threading
import time
import unittest

import trio

from app import ApplicationRuntime
from app.events import Event, EventBus, EventType
from api.base import as_runtime


class _ReadyService:
    """Duck-typed engine double (no host, no UI)."""

    def __init__(self):
        self.ready = False
        self.running = False
        self.failed = False
        self.stop_event = threading.Event()
        self.nickname = "fake"
        self.port = 41234
        self.topic = None
        self.strict_signing = True
        self.download_dir = "/tmp"
        self.transport_addrs = []
        self.full_multiaddrs = []
        self.connect_addrs = []

    async def start(self):
        self.ready = True
        self.running = True

    def request_stop(self):
        self.stop_event.set()

    def get_connection_info(self):
        return {
            "peer_id": "peer-fake",
            "nickname": self.nickname,
            "multiaddr": None,
            "multiaddrs": [],
            "listen_addrs": [],
            "connected_peers": set(),
            "peer_count": 0,
        }


class _FailingService(_ReadyService):
    async def start(self):
        self.failed = True
        raise RuntimeError("simulated startup failure")


class EventBusTests(unittest.TestCase):
    def test_type_filter(self):
        bus = EventBus()
        subscription = bus.subscribe([EventType.SYSTEM])
        bus.publish(Event(EventType.MESSAGE_RECEIVED, {"message": "hi"}))
        self.assertIsNone(subscription.get_nowait())
        bus.publish(Event(EventType.SYSTEM, {"message": "system"}))
        event = subscription.get_nowait()
        self.assertEqual(event.type, EventType.SYSTEM)

    def test_full_queue_drops_oldest_instead_of_blocking(self):
        bus = EventBus(maxsize=2)
        subscription = bus.subscribe()
        for index in range(3):
            bus.publish(Event(EventType.SYSTEM, {"index": index}))
        self.assertEqual(subscription.get_nowait().data["index"], 1)
        self.assertEqual(subscription.get_nowait().data["index"], 2)
        self.assertEqual(subscription.dropped, 1)

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        subscription = bus.subscribe()
        bus.unsubscribe(subscription)
        bus.publish(Event(EventType.SYSTEM, {"index": 1}))
        self.assertTrue(subscription.closed)
        self.assertIsNone(subscription.get_nowait())

    def test_publish_never_blocks_with_no_consumer(self):
        bus = EventBus(maxsize=1)
        bus.subscribe()
        started = time.monotonic()
        for index in range(1000):
            bus.publish(Event(EventType.MESSAGE_RECEIVED, {"index": index}))
        self.assertLess(time.monotonic() - started, 1.0)


class RuntimeTests(unittest.TestCase):
    def test_services_share_one_engine_and_event_bus(self):
        service = _ReadyService()
        runtime = ApplicationRuntime(service=service)
        self.assertIs(runtime.service, service)
        self.assertIs(service.event_bus, runtime.events)
        self.assertIs(runtime.messaging.engine, service)
        self.assertIs(runtime.peer.engine, service)

    def test_status_reports_engine_state(self):
        runtime = ApplicationRuntime(service=_ReadyService())
        status = runtime.status()
        self.assertEqual(status.nickname, "fake")
        self.assertEqual(status.port, 41234)
        self.assertEqual(status.state, "created")

    def test_start_twice_raises(self):
        runtime = ApplicationRuntime(service=_ReadyService())
        trio.run(runtime.start)
        with self.assertRaisesRegex(RuntimeError, "already been called"):
            trio.run(runtime.start)

    def test_start_in_thread_reaches_ready(self):
        runtime = ApplicationRuntime(service=_ReadyService())
        runtime.start_in_thread(timeout=5)
        self.assertTrue(runtime.ready)
        self.assertTrue(runtime.join(5))
        runtime.close()

    def test_startup_failure_cleans_up_and_closes_subscribers(self):
        service = _FailingService()
        runtime = ApplicationRuntime(service=service)
        subscription = runtime.subscribe()
        with self.assertRaisesRegex(RuntimeError, "failed to start"):
            runtime.start_in_thread(timeout=5)
        self.assertTrue(runtime.failed)
        self.assertTrue(subscription.closed)
        self.assertTrue(runtime.join(5))

    def test_as_runtime_reuses_one_runtime_for_a_bare_service(self):
        service = _ReadyService()
        first = as_runtime(service)
        second = as_runtime(service)
        self.assertIs(first, second)
        self.assertIs(first.service, service)


if __name__ == "__main__":
    unittest.main()
