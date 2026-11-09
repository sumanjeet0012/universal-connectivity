"""
Headless Service for Universal Connectivity Python Peer

This module provides a headless service that manages libp2p host, pubsub, and chat functionality
without any UI. It communicates with the UI through queues and events.
"""

import json
import ipaddress
import errno
import functools
import logging
import os
import socket
import threading
import time
import traceback
import multiaddr
import janus
import trio
import trio_asyncio
import hashlib
from pathlib import Path
from queue import Empty, Full
from typing import List, Dict, Any, Set
from libp2p.discovery.bootstrap import BootstrapDiscovery
from libp2p.kad_dht.kad_dht import (
    DHTMode,
    KadDHT,
)
from libp2p import new_host, IHost
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.identity_utils import (
    create_new_ed25519_key_pair,
    load_identity,
    save_identity,
)
from libp2p.pubsub.gossipsub import GossipSub
from libp2p.pubsub.pubsub import Pubsub
from libp2p.tools.anyio_service import background_trio_service
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.identity.identify.identify import identify_handler_for, parse_identify_response, ID as IDENTIFY_PROTOCOL_ID
from libp2p.utils.varint import read_length_prefixed_protobuf
from libp2p.peer.id import ID
from libp2p.custom_types import TProtocol
from libp2p.pubsub.gossipsub import PROTOCOL_ID, PROTOCOL_ID_V11, PROTOCOL_ID_V12
from libp2p.protocol_muxer.exceptions import (
    MultiselectClientError,
)
from libp2p.host.exceptions import (
    StreamFailure,
)
from chatroom import (
    ChatRoom,
    ChatMessage,
    PUBSUB_DISCOVERY_TOPIC,
    MAX_TOPIC_NAME_LENGTH,
    MAX_TOPICS,
)
from app.storage import InMemoryMessageStore, message_identity
from libp2p.bitswap import BitswapClient, MemoryBlockStore
from libp2p.bitswap.dag import MerkleDag
from libp2p.network.config import ConnectionConfig

# File message prefix for pubsub
FILE_MESSAGE_PREFIX = "[FILE]"
# Default download directory
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

# Resource bounds. The API and the network are untrusted inputs: without caps
# a slow client, a busy topic, or a message flood can grow these structures
# until the peer is OOM-killed.
MAX_TOPIC_MESSAGES = 1000        # stored history per topic
MAX_LEGACY_QUEUE = 1024          # UI compatibility queue
MAX_OUTGOING_QUEUE = 1024        # API/UI -> chat publisher
MAX_REQUEST_QUEUE = 256          # topic/peer requests
MAX_FILE_QUEUE = 64              # file share/download requests

logger = logging.getLogger("headless")

# Constants
DISCOVERY_SERVICE_TAG = "universal-connectivity"
PROTOCOL_ID_LIST = [PROTOCOL_ID, PROTOCOL_ID_V11, PROTOCOL_ID_V12]
DEFAULT_PORT = 9095


def _write_bytes(path: str, data: bytes) -> None:
    """Blocking file write, called from a worker thread when possible."""
    with open(path, "wb") as f:
        f.write(data)


def _save_identity_atomically(key_pair, path: Path) -> None:
    """Persist a private key without leaving a half-written file behind.

    ``save_identity`` writes the libp2p protobuf format and sets mode 0600 on
    POSIX. Writing to a sibling temp file and renaming means a crash or a full
    disk leaves the previous identity intact instead of an unreadable key.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        save_identity(key_pair, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass

# Bootstrap nodes for peer discovery
BOOTSTRAP_PEERS = [
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmNnooDu7bfjPFoTZYxMNLWUQJyrVwtbZg5gBMjTezGAJN",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmQCU2EcMqAqQPR2i9bChDtGNJchTbq5TbXJJ16u19uLTa",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmbLHAnMoJPWSCR5Zp7ykQCj2gRNdrFeqQ1vG13rMb4sPS",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmcZf59bWwK5XFi76CZX8cbJ4BhTzzA3gU1ZjYZcYW3dwt",
    "/ip4/104.131.131.82/tcp/4001/p2p/QmaCpDMGvV2BGHeYERUEnRQAwe3N8SzbUtfsmvsqQLuvuJ",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmbLHAnMoJPWSCR5Zhtx6BHJX9KiKNN6tpvbUcqanj75Nb"
]

def filter_compatible_peer_info(peer_info) -> bool:
    """Filter peer info to check if it has compatible addresses (TCP + IPv4)."""
    if not hasattr(peer_info, "addrs") or not peer_info.addrs:
        return False

    for addr in peer_info.addrs:
        addr_str = str(addr)
        if "/tcp/" in addr_str and "/ip4/" in addr_str and "/quic" not in addr_str:
            return True
    return False


class HeadlessService:
    """
    Headless service that manages libp2p components and provides data to UI through queues.
    """

    def __init__(self, nickname: str, port: int = 0, connect_addrs: List[str] = None, ui_mode: bool = False, strict_signing: bool = True, seed: str = None, topic: str = None, announce_host: str = "127.0.0.1", listen_addrs: List[str] = None, announce_addrs: List[str] = None, message_store=None, event_bus=None, identity_path: str = None):
        self.nickname = nickname
        self.port = port if port != 0 else 4001
        self.connect_addrs = connect_addrs or []

        # Address model (deliberately separate concepts):
        #   listen  : where sockets are opened. Default binds all IPv4
        #             interfaces on the configured port.
        #   announce: what host.get_addrs() shares with other peers. Explicit
        #             announce_addrs win; otherwise wildcard listen addresses
        #             are rewritten to announce_host (0.0.0.0 is never
        #             advertised because it is not a dial target).
        #   observed: addresses reported by Identify and recorded by
        #             py-libp2p's ObservedAddrManager, then merged into
        #             get_addrs() by the library.
        self.listen_addrs = list(listen_addrs) if listen_addrs else None
        self.announce_addrs = list(announce_addrs) if announce_addrs else None
        # Host advertised in the multiaddr shared with other peers. The listen
        # socket binds 0.0.0.0, but 0.0.0.0 is not a valid dial target, so
        # advertise a concrete address instead (override with --announce-host
        # for LAN access, or --announce-addr for full control).
        self.announce_host = announce_host
        self.event_bus = event_bus  # optional app.events.EventBus; additive
        self.ui_mode = ui_mode  # Flag to control logging behavior
        self.strict_signing = strict_signing  # Flag to control message signing
        # Identity: an explicit --seed is a deliberate deterministic identity
        # for development; otherwise the runtime injects a persisted key file
        # under the data directory (see _load_identity).
        self.seed = seed or None
        self.identity_path = (
            os.path.abspath(os.path.expanduser(identity_path))
            if identity_path
            else None
        )
        self.identity_source = None  # seed | persisted | created | ephemeral
        self.topic = topic  # Custom topic to use instead of default

        # libp2p components
        self.host : IHost = None
        self.pubsub = None
        self.gossipsub = None
        self.dht = None
        self.chat_room = None
        
        # Bitswap components for file sharing
        self.bitswap_client = None
        self.merkle_dag = None
        self.block_store = MemoryBlockStore()
        
        # Service state
        self.running = False
        self.ready = False
        self.full_multiaddr = None
        self.full_multiaddrs = []
        self.transport_addrs = []  # raw bound listener addresses (may include 0.0.0.0)
        
        # Communication with UI
        self.message_queue = None  # UI receives messages from headless
        self.system_queue = None   # UI receives system messages from headless
        self.outgoing_queue = None # UI sends messages to headless
        self.topic_subscription_queue = None  # UI sends topic subscription requests
        self.peer_connection_queue = None  # UI sends peer connection requests
        self.file_share_queue = None  # UI sends file sharing requests
        self.file_download_queue = None  # UI sends file download requests

        # Per-WebSocket-client fan-out queues. The single message_queue above is
        # consumed by whichever client polls first; API WebSocket clients each
        # register their own queue so every client sees every message.
        self.message_subscribers = set()
        self.system_subscribers = set()
        
        # Per-topic message storage. Stable identity, dedupe, and retention
        # live in the repository so the same code path serves the default
        # in-memory store and a durable SQLite store (injected by the runtime).
        self.message_store = message_store or InMemoryMessageStore()
        
        # File sharing state
        self.shared_files = {}  # {cid_hex: {'filename': str, 'filesize': int, 'filepath': str}}
        self.download_dir = DEFAULT_DOWNLOAD_DIR
        os.makedirs(self.download_dir, exist_ok=True)
        
        # Peer information storage for identify protocol
        self.peer_info_cache = {}  # Store peer info retrieved through identify
        
        # Events for synchronization. stop_event must be settable from the
        # Tornado/API thread, so it is a threading.Event rather than a trio.Event.
        self.ready_event = trio.Event()
        self.stop_event = threading.Event()
        
        # Optional callback fired once the service has fully stopped, so hosts
        # such as the Tornado API server can shut themselves down too.
        self.on_stopped = None
        # True when the service died from an error (as opposed to a clean stop),
        # so the process can exit non-zero and be restarted by its supervisor.
        self.failed = False

        # Lifecycle ownership. start() may run at most once per instance, and
        # the async teardown runs exactly once from start()'s finally block,
        # on the trio thread that owns the host.
        self._started = False
        self._shutdown_done = False

        # Guards shared state that is written by the trio thread and read by
        # Tornado handlers (topic history, shared files, identify cache) and
        # the WebSocket subscriber sets. Python's GIL makes individual dict
        # operations atomic, but not iteration or check-then-act sequences.
        self._state_lock = threading.RLock()
        self._subscribers_lock = threading.RLock()

        # Snapshot used to derive peer connect/disconnect application events.
        self._known_connected_peers: set[str] = set()

        if not ui_mode:  # Only log initialization if not in UI mode
            logger.info(f"HeadlessService initialized - nickname: {nickname}, port: {self.port}, strict_signing: {strict_signing}")
    
    async def monitor_peers(self):
        while True:
            try:
                current = {str(peer) for peer in self.host.get_connected_peers()}
                previous = self._known_connected_peers
                for peer_id in sorted(current - previous):
                    self._publish_event("peer_connected", {"peer_id": peer_id})
                for peer_id in sorted(previous - current):
                    self._publish_event("peer_disconnected", {"peer_id": peer_id})
                self._known_connected_peers = current
                logger.info(f"Connected peers are: {len(current)}")
                logger.info(f"Peers in peer store are: {len(self.host.get_peerstore().peers_with_addrs())}")
                logger.info(f"Peers in routing table are: {len(self.dht.routing_table.get_peer_ids())}")
                logger.info(f"Peers in pubsub are: {len(self.pubsub.peers.keys())}")
            except Exception as e:
                # A status-logging failure must never take down the node.
                logger.warning(f"Peer monitor error: {e}")
            await trio.sleep(5)

    async def start(self):
        """Start the headless service."""
        if self._started:
            raise RuntimeError(
                "HeadlessService.start() has already been called on this instance"
            )
        self._started = True
        logger.info("Starting headless service...")
        
        try:
            # Create queues for communication with UI
            logger.debug("Creating message queues...")
            self.message_queue = janus.Queue(maxsize=MAX_LEGACY_QUEUE)  # Messages from headless to UI
            self.system_queue = janus.Queue(maxsize=MAX_LEGACY_QUEUE)   # System messages from headless to UI
            self.outgoing_queue = janus.Queue(maxsize=MAX_OUTGOING_QUEUE)  # Messages from UI to headless
            self.topic_subscription_queue = janus.Queue(maxsize=MAX_REQUEST_QUEUE)  # Topic subscription requests
            self.peer_connection_queue = janus.Queue(maxsize=MAX_REQUEST_QUEUE)  # Peer connection requests
            self.file_share_queue = janus.Queue(maxsize=MAX_FILE_QUEUE)  # File sharing requests
            self.file_download_queue = janus.Queue(maxsize=MAX_FILE_QUEUE)  # File download requests
            logger.debug("Message queues created successfully")
            
            # Enable trio-asyncio mode
            async with trio_asyncio.open_loop():
                # Send initial system message to test queue inside trio context
                await self._send_system_message("Headless service starting...")
                await self._run_service()
                    
        except Exception as e:
            self.failed = True
            logger.error(f"Failed to start headless service: {e}")
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            raise
        finally:
            # Perform teardown on the trio thread that owns the host, whatever
            # the exit path (stop requested, startup error, or cancellation).
            # The shield keeps cleanup running if the caller's scope was
            # cancelled while awaiting readiness.
            with trio.CancelScope(shield=True):
                await self._shutdown_async()
            # Whatever the exit path, the node is no longer serving: make the
            # API report not-ready instead of leaving stale "ready: true".
            self.ready = False
            self.running = False
            if self.on_stopped:
                try:
                    self.on_stopped()
                except Exception:
                    logger.exception("Error in on_stopped callback")
    
    def _warn_if_port_in_use(self, listen_addrs):
        """
        Warn when a listen port is already taken by another process.

        py-libp2p enables SO_REUSEPORT (for DCUtR hole punching), so a second
        peer can bind the same port without error. Inbound connections are then
        delivered to whichever process the kernel picks, which surfaces as
        "handshake failed when upgrading to secure connection" because a peer
        with a different PeerID answers. A plain SO_REUSEADDR probe bind still
        fails while a listener is active, so we can detect the situation and
        tell the user to pick a different port.
        """
        probed = set()
        for addr in listen_addrs:
            protocols = {p.name for p in addr.protocols()}
            if "tcp" not in protocols or "ip4" not in protocols:
                continue
            host = addr.value_for_protocol("ip4")
            port = int(addr.value_for_protocol("tcp"))
            bind_host = "0.0.0.0" if host == "0.0.0.0" else host
            if (bind_host, port) in probed:
                continue
            probed.add((bind_host, port))

            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((bind_host, port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    logger.warning(
                        f"⚠️  Port {port} on {bind_host} is already in use by "
                        "another process. libp2p uses SO_REUSEPORT, so this peer "
                        "will still bind, but incoming connections may be handled "
                        "by the other process and fail with 'handshake failed when "
                        "upgrading to secure connection'. Start each local peer "
                        "with a distinct port, e.g. --port 4001 / --port 4002."
                    )
                elif exc.errno == errno.EADDRNOTAVAIL:
                    logger.warning(
                        f"Listen host {bind_host}:{port} is not assigned to this "
                        "machine; the listener will fail to start."
                    )
                else:
                    logger.warning(
                        f"Could not probe listen address {bind_host}:{port}: {exc}"
                    )
            finally:
                probe.close()

    # Transports this build can actually construct. WebSocket is omitted
    # because the `websockets` dependency is not installed; WebRTC and
    # WebTransport need extra configuration (and `aiortc` for WebRTC).
    # QUIC is intentionally excluded: it works experimentally (see
    # docs/architecture-audit.md §15.1) but enabling it changes the transport
    # mix and is out of Phase 1 scope, so it is rejected explicitly instead of
    # being silently accepted without a registered transport.
    _UNSUPPORTED_LISTEN_PROTOCOLS = (
        "quic",
        "quic-v1",
        "ws",
        "wss",
        "webrtc",
        "webrtc-direct",
        "webtransport",
    )

    def _resolve_listen_addrs(self) -> List[multiaddr.Multiaddr]:
        """Parse and validate the configured listen addresses."""
        raw = self.listen_addrs or [f"/ip4/0.0.0.0/tcp/{self.port}"]
        resolved = []
        for item in raw:
            try:
                addr = multiaddr.Multiaddr(item)
            except Exception as exc:
                raise ValueError(f"Invalid listen address {item!r}: {exc}") from exc
            protocols = {p.name for p in addr.protocols()}
            unsupported = [p for p in self._UNSUPPORTED_LISTEN_PROTOCOLS if p in protocols]
            if unsupported:
                raise ValueError(
                    f"Unsupported listen address {item!r}: transport "
                    f"{unsupported[0]!r} is not enabled in this build "
                    "(plain TCP only)"
                )
            if "tcp" not in protocols:
                raise ValueError(
                    f"Unsupported listen address {item!r}: expected a TCP multiaddr"
                )
            resolved.append(addr)
        return resolved

    def _resolve_announce_addrs(self) -> List[multiaddr.Multiaddr]:
        """Parse explicit announce addresses and reject undialable wildcards."""
        if not self.announce_addrs:
            return []
        resolved = []
        for item in self.announce_addrs:
            try:
                addr = multiaddr.Multiaddr(item)
            except Exception as exc:
                raise ValueError(
                    f"Invalid announce address {item!r}: {exc}"
                ) from exc
            protocols = {p.name for p in addr.protocols()}
            if "ip4" in protocols and addr.value_for_protocol("ip4") == "0.0.0.0":
                raise ValueError(
                    f"Announce address {item!r} uses the wildcard host 0.0.0.0; "
                    "use a concrete dialable IP"
                )
            if "ip6" in protocols and addr.value_for_protocol("ip6") in ("::", "::0"):
                raise ValueError(
                    f"Announce address {item!r} uses the wildcard host ::; "
                    "use a concrete dialable IP"
                )
            resolved.append(addr)
        return resolved

    def _make_addrs_factory(self):
        """Return an AddrsFactory that never advertises a wildcard address.

        The factory only rewrites wildcard listeners (the socket is bound on
        all interfaces, so a concrete host is valid). A listener explicitly
        bound to a concrete address is advertised as-is; if that address is
        loopback, the caller gets a clear warning rather than a silent rewrite
        that would point at an interface the peer is not listening on.
        """
        if self.announce_addrs:
            return None  # explicit static addresses win
        host = (self.announce_host or "").strip()
        if not host:
            return None
        if host in ("0.0.0.0", "::", "::0"):
            raise ValueError(
                "announce_host cannot be a wildcard address; use a concrete "
                "dialable IP (or an explicit --announce-addr)"
            )
        announce_is_v6 = ":" in host

        def _factory(candidates):
            advertised = []
            seen = set()
            for addr in candidates:
                text = str(addr)
                protocols = {p.name for p in addr.protocols()}
                if "ip4" in protocols:
                    value = addr.value_for_protocol("ip4")
                    if value == "0.0.0.0":
                        if announce_is_v6:
                            continue
                        text = text.replace(
                            "/ip4/0.0.0.0/", f"/ip4/{host}/", 1
                        )
                elif "ip6" in protocols:
                    value = addr.value_for_protocol("ip6")
                    if value in ("::", "::0"):
                        if not announce_is_v6:
                            continue
                        text = text.replace("/ip6/::/", f"/ip6/{host}/", 1)
                if text not in seen:
                    seen.add(text)
                    advertised.append(multiaddr.Multiaddr(text))
            return advertised

        return _factory

    @staticmethod
    def _addr_match_key(addr):
        """Normalize a multiaddr for listener comparison.

        A requested port of 0 means "ephemeral"; it is represented as ``None``
        so it matches whatever port the OS assigned.
        """
        protocols = {p.name for p in addr.protocols()}
        family = "ip4" if "ip4" in protocols else "ip6" if "ip6" in protocols else None
        host = addr.value_for_protocol(family) if family else None
        port = None
        for proto in ("tcp", "udp"):
            if proto in protocols:
                port = int(addr.value_for_protocol(proto))
                break
        return (family, host, None if port == 0 else port)

    def _verify_listeners(self, requested: List[multiaddr.Multiaddr]) -> None:
        """Verify that every requested listener actually bound."""
        bound = [str(addr) for addr in self.host.get_transport_addrs()]
        self.transport_addrs = bound
        if not bound:
            raise RuntimeError("libp2p host did not start any listener")
        bound_keys = [self._addr_match_key(multiaddr.Multiaddr(item)) for item in bound]
        missing = []
        for req in requested:
            family, host, port = self._addr_match_key(req)
            matched = any(
                b_family == family
                and b_host == host
                and (port is None or b_port == port)
                for b_family, b_host, b_port in bound_keys
            )
            if not matched:
                missing.append(str(req))
        if missing:
            raise RuntimeError(
                "libp2p failed to start listener(s): " + ", ".join(missing)
            )
        logger.info(f"Listener(s) active: {', '.join(bound)}")

    @staticmethod
    def _is_wildcard_addr(addr) -> bool:
        """True when a multiaddr binds a wildcard host (0.0.0.0 / ::)."""
        protocols = {p.name for p in addr.protocols()}
        if "ip4" in protocols:
            return addr.value_for_protocol("ip4") == "0.0.0.0"
        if "ip6" in protocols:
            return addr.value_for_protocol("ip6") in ("::", "::0")
        return False

    @staticmethod
    def _classify_announced_addr(addr_str: str) -> str:
        """Describe address reachability without claiming it was tested."""
        try:
            addr = multiaddr.Multiaddr(addr_str)
            protocols = {p.name for p in addr.protocols()}
            if "ip4" in protocols:
                ip = ipaddress.ip_address(addr.value_for_protocol("ip4"))
            elif "ip6" in protocols:
                ip = ipaddress.ip_address(addr.value_for_protocol("ip6"))
            else:
                return "non-IP"
        except Exception:
            return "unparsed"
        if ip.is_unspecified:
            return "wildcard (not dialable)"
        if ip.is_loopback:
            return "loopback (same-machine only)"
        if ip.is_link_local:
            return "link-local (same link only)"
        if ip.is_private:
            return "private/LAN (reachable only on that network)"
        return "public/unknown (reachability not verified)"

    def _refresh_advertised_addrs(self) -> None:
        """Snapshot and log host.get_addrs() after listeners are ready."""
        advertised = [str(addr) for addr in self.host.get_addrs()]
        self.full_multiaddrs = advertised
        self.full_multiaddr = advertised[0] if advertised else None
        if not advertised:
            logger.warning(
                "No advertised addresses: this peer can dial out, but has no "
                "address to share. Check --listen-addr / --announce-host / "
                "--announce-addr (the address family must match)."
            )
            return
        for addr in advertised:
            category = self._classify_announced_addr(addr)
            logger.info(f"Advertised address [{category}]: {addr}")
            if category.startswith("loopback"):
                logger.warning(
                    "Advertising a loopback address: only peers on this machine "
                    "can dial it. Use --announce-host <LAN IP> or "
                    "--announce-addr <multiaddr> for other peers."
                )

    @staticmethod
    def _normalize_dial_addr(multiaddr_str: str) -> str:
        """
        Rewrite wildcard hosts to loopback so pasted addresses are dialable.

        Peers advertise their listen address as /ip4/0.0.0.0/... in older
        versions; 0.0.0.0 only makes sense for binding, never for dialing.
        """
        normalized = multiaddr_str
        if "/ip4/0.0.0.0/" in normalized:
            normalized = normalized.replace("/ip4/0.0.0.0/", "/ip4/127.0.0.1/")
        if "/ip6/::/" in normalized:
            normalized = normalized.replace("/ip6/::/", "/ip6/::1/")
        if normalized != multiaddr_str:
            logger.warning(
                f"Dial address contains a wildcard host; using loopback instead: "
                f"{multiaddr_str} -> {normalized}"
            )
        return normalized

    def _load_identity(self):
        """Return the key pair this host runs with.

        Resolution order is deliberate, because the peer ID is derived from the
        key and other peers dial and address us by it:

        1. An explicit ``--seed`` uses a deterministic key and never touches
           disk. This keeps the historical development/test behaviour and lets
           callers request a stable peer ID.
        2. An existing identity file is loaded as-is. This is what makes the
           peer ID survive a restart, even if ``--seed`` is added or removed.
        3. No file yet generates one fresh Ed25519 key and persists it with
           mode 0600.

        Without a data directory the identity is ephemeral and the peer ID
        changes on restart; that is logged as a warning. A corrupt identity
        file fails startup instead of silently minting a different peer ID.
        """
        if self.seed:
            self.identity_source = "seed"
            logger.info(
                "Using a deterministic identity from an explicit --seed "
                "(the seed value is not logged)"
            )
            return create_new_key_pair(
                seed=hashlib.sha256(self.seed.encode()).digest()
            )

        path = Path(self.identity_path) if self.identity_path else None
        if path is not None and path.is_file():
            try:
                key_pair = load_identity(path)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Stored peer identity is unreadable: {path} ({exc}). "
                    "Restore it from backup, or delete it to create a new "
                    "identity (this changes the peer ID other peers dial)."
                ) from exc
            self.identity_source = "persisted"
            logger.info(f"Loaded persisted peer identity from {path}")
            return key_pair

        key_pair = create_new_ed25519_key_pair()
        if path is None:
            self.identity_source = "ephemeral"
            logger.warning(
                "No data directory configured; using an ephemeral peer "
                "identity, so the peer ID will change on restart. Pass "
                "--data-dir or set UC_DATA_DIR to keep it."
            )
            return key_pair

        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _save_identity_atomically(key_pair, path)
        except OSError as exc:
            raise RuntimeError(
                f"Could not persist the peer identity at {path}: {exc}. "
                "Choose a writable data directory with --data-dir or "
                "UC_DATA_DIR."
            ) from exc
        self.identity_source = "created"
        logger.info(f"Created a new peer identity at {path} (mode 0600)")
        return key_pair

    async def _run_service(self):
        """Run the main service loop."""
        key_pair = self._load_identity()

        # Resolve bind vs advertised addresses before creating the host. The
        # listen list is verified after host.run() starts the listeners; the
        # advertised list is derived from host.get_addrs() at the same point.
        listen_addrs = self._resolve_listen_addrs()
        explicit_announce = self._resolve_announce_addrs()
        addrs_factory = self._make_addrs_factory()

        self._warn_if_port_in_use(listen_addrs)

        config = ConnectionConfig(
        min_connections=10,
        low_watermark=12,
        high_watermark=40,
        max_connections=50,
        auto_connect_interval=5.0,  # Check every 5 seconds
        )
        
        # Create libp2p host WITHOUT bootstrap nodes initially
        # We'll connect to bootstrap nodes after pubsub is running
        self.host = new_host(
            key_pair=key_pair,
            connection_config=config,
            # Pass the listen list so libp2p auto-detects the transports it must
            # register (TCP today; QUIC when a QUIC listen address is supplied).
            listen_addrs=listen_addrs,
            announce_addrs=explicit_announce or None,
            addrs_factory=addrs_factory,
            # bootstrap = BOOTSTRAP_PEERS
        )

        # Register identify protocol handler
        logger.info("📋 Registering identify protocol handler (raw protobuf format for go-libp2p compatibility)")
        identify_handler = identify_handler_for(self.host, use_varint_format=True)
        self.host.set_stream_handler(IDENTIFY_PROTOCOL_ID, identify_handler)
        logger.info(f"✅ Identify protocol handler registered for {IDENTIFY_PROTOCOL_ID} (raw format)")

        # Create DHT with random walk enabled
        self.dht = KadDHT(self.host, DHTMode.SERVER, enable_random_walk=True)
        logger.info("✅ DHT created with random walk enabled")
        
        logger.info(f"Host created with PeerID: {self.host.get_id()}")
        logger.info(
            "Configured listen address(es): "
            + ", ".join(str(addr) for addr in listen_addrs)
        )
        if explicit_announce:
            logger.info(
                "Using explicit announce address(es): "
                + ", ".join(str(addr) for addr in explicit_announce)
            )
        elif any(self._is_wildcard_addr(addr) for addr in listen_addrs):
            logger.info(
                f"Wildcard listeners will be advertised as {self.announce_host!r} "
                "(configure --announce-host or --announce-addr to change)"
            )
        else:
            logger.info(
                "No wildcard listeners: advertised addresses are the concrete "
                "bound addresses from the host"
            )
        
        # Log GossipSub protocol configuration
        logger.info(f"📋 Configuring GossipSub with protocols: {PROTOCOL_ID_LIST}")
        logger.info(f"  Protocol 1: {PROTOCOL_ID}")
        logger.info(f"  Protocol 2: {PROTOCOL_ID_V11}")
        
        # Create GossipSub with optimized parameters (matching working pubsub.py)
        self.gossipsub = GossipSub(
            protocols=PROTOCOL_ID_LIST,
            degree=3,
            degree_low=2,
            degree_high=4,
            gossip_window=2,  # Smaller window for faster gossip
            gossip_history=5,  # Keep more history
            heartbeat_initial_delay=2.0,  # Start heartbeats sooner
            heartbeat_interval=5,  # More frequent heartbeats for testing
        )
        logger.info("✅ GossipSub router created successfully")
        
        # Create PubSub
        logger.info(f"🔐 Creating PubSub with strict_signing={self.strict_signing}")
        self.pubsub = Pubsub(self.host, self.gossipsub, strict_signing=self.strict_signing)
        logger.info("✅ PubSub service created successfully")
        
        # Create Bitswap client for file sharing
        self.bitswap_client = BitswapClient(self.host, self.block_store)
        self.merkle_dag = MerkleDag(self.bitswap_client)
        logger.info("✅ Bitswap client and MerkleDag created for file sharing")
        
        # Start host and pubsub services
        async with self.host.run(listen_addrs=listen_addrs):
            # Listeners are bound before host.run() yields. Verify that the
            # requested addresses really started, then derive the advertised
            # list from the live host (never from a precomputed string).
            self._verify_listeners(listen_addrs)
            self._refresh_advertised_addrs()
            logger.info("📡 Initializing PubSub, GossipSub, and DHT services...")
            try:
                async with background_trio_service(self.pubsub):
                    async with background_trio_service(self.gossipsub):
                        async with background_trio_service(self.dht):
                            logger.info("✅ Pubsub, GossipSub, and DHT services started.")
                            await self.pubsub.wait_until_ready()
                            logger.info("✅ Pubsub ready and operational.")
                            logger.info("✅ DHT service started with random walk enabled.")
                            bootstrap = None
                            if BOOTSTRAP_PEERS:
                                bootstrap = BootstrapDiscovery(self.host.get_network(), BOOTSTRAP_PEERS)
                                await bootstrap.start()
                            # Setup chat room BEFORE connections so topics are subscribed
                            # This ensures GossipSub protocol negotiation succeeds when connecting
                            await self._setup_chat_room()
                            
                            # Start Bitswap client
                            await self.bitswap_client.start()
                            logger.info("✅ Bitswap client started for file sharing")
                            
                            # Now setup connections after we're subscribed to topics
                            await self._setup_connections()
                            
                            # Setup connection event handlers for DHT
                            
                            # Mark service as ready
                            self.ready = True
                            self.ready_event.set()
                            logger.info("✅ Headless service is ready")
                            
                            # Start message processing and wait for stop
                            async with trio.open_nursery() as nursery:
                                # Set nursery for bitswap client
                                self.bitswap_client.set_nursery(nursery)
                                
                                nursery.start_soon(self._process_messages)
                                nursery.start_soon(self._process_outgoing_messages)
                                nursery.start_soon(self._process_topic_subscriptions)
                                nursery.start_soon(self._process_peer_connections)
                                nursery.start_soon(self._process_file_shares)
                                nursery.start_soon(self._process_file_downloads)
                                nursery.start_soon(self.monitor_peers)
                                # nursery.start_soon(maintain_connections, self.host)

                                # Block until a stop is requested from any thread,
                                # then cancel everything (message handlers can be
                                # parked on subscription.get() and would otherwise
                                # keep the nursery — and the host — alive forever).
                                await self._wait_for_stop()
                                nursery.cancel_scope.cancel()

            except (MultiselectClientError, StreamFailure) as e:
                # These are not routine connection errors: _setup_connections()
                # already handles per-peer dial failures. Reaching this outer
                # scope means a core protocol (pubsub/DHT/identify) failed, so
                # mark the service as failed and let the supervisor restart it
                # instead of silently exiting as if the stop was clean.
                logger.error(f"The protocol negotiation failed: {e}")
                self.failed = True
                raise
    
    async def _setup_connections(self):
        """Setup connections to specified peers with detailed protocol logging."""
        if not self.connect_addrs:
            return
        
        for addr_str in self.connect_addrs:
            try:
                logger.info(f"🔗 Attempting to connect to: {addr_str}")
                maddr = multiaddr.Multiaddr(self._normalize_dial_addr(addr_str))
                info = info_from_p2p_addr(maddr)
                logger.info(f"🔗 Parsed peer info - ID: {info.peer_id}, Addrs: {info.addrs}")
                
                # Check if already connected
                existing_conns = self.host.get_network().connections.get(info.peer_id)
                if existing_conns:
                    logger.info(f"✅ Already connected to peer: {info.peer_id}, skipping connection attempt")
                    continue
                
                # Log connection attempt
                logger.info(f"🔗 Initiating connection to peer: {info.peer_id}")
                await self.host.connect(info)
                logger.info(f"✅ TCP connection established to peer: {info.peer_id}")
                
                # Wait longer for protocol negotiation
                await trio.sleep(3)
                
                # Detailed protocol inspection
                logger.info(f"🔍 Starting protocol inspection for peer: {info.peer_id}")
                await self._inspect_peer_protocols(info.peer_id)
                
                # Check connection status
                try:
                    # In py-libp2p, we can check if peer is connected via the swarm
                    swarm = self.host.get_network()
                    if hasattr(swarm, 'connections') and info.peer_id in swarm.connections:
                        connections = [swarm.connections[info.peer_id]]
                        logger.info(f"📊 Active connections to peer {info.peer_id}: {len(connections)}")
                    else:
                        logger.info(f"📊 No direct connection info available for peer {info.peer_id}")
                except Exception as conn_err:
                    logger.warning(f"⚠️  Could not check connection status: {conn_err}")
                
                # Wait for PubSub protocol negotiation
                logger.info(f"⏳ Waiting for PubSub protocol negotiation...")
                await trio.sleep(3)
                
                # Check final PubSub status
                await self._check_pubsub_status(info.peer_id)
                
                await self._send_system_message(f"Connected to peer: {str(info.peer_id)[:8]}")
                
            except Exception as e:
                logger.error(f"❌ Failed to connect to {addr_str}: {e}")
                await self._send_system_message(f"Failed to connect to {addr_str}: {e}")
    
    async def _inspect_peer_protocols(self, peer_id):
        """Inspect and log all protocols supported by a peer."""
        try:
            logger.info(f"🔍 Checking peerstore for peer: {peer_id}")
            
            # Get peer's protocols from peerstore (simplified approach)
            peerstore = self.host.get_peerstore()
            
            # Check if we can access protocols - different py-libp2p versions have different APIs
            try:
                if hasattr(peerstore, 'get_protocols'):
                    protocols = peerstore.get_protocols(peer_id)
                elif hasattr(peerstore, 'protocols'):
                    protocols = peerstore.protocols(peer_id)
                else:
                    # Fallback - just log that we connected successfully
                    logger.info(f"✅ Successfully connected to peer {peer_id}")
                    logger.info(f"🔍 Protocol inspection not available in this py-libp2p version")
                    return
                    
                if protocols:
                    logger.info(f"📋 Peer {peer_id} supports {len(protocols)} protocols:")
                    for i, protocol in enumerate(protocols, 1):
                        logger.info(f"  {i}: {protocol}")
                        if "meshsub" in str(protocol) or "gossipsub" in str(protocol):
                            logger.info(f"  🎯 Found PubSub protocol: {protocol}")
                else:
                    logger.info(f"📋 No protocols found for peer {peer_id} yet (may still be negotiating)")
                    
            except Exception as proto_err:
                logger.info(f"🔍 Protocol details not accessible: {proto_err}")
                logger.info(f"✅ Peer {peer_id} connected successfully")
                    
        except Exception as e:
            logger.warning(f"⚠️  Error inspecting peer protocols: {e}")
            logger.info(f"✅ Peer {peer_id} connected successfully")
    
    async def _check_pubsub_status(self, peer_id):
        """Check the PubSub connection status with a specific peer."""
        try:
            logger.info(f"🔍 Checking PubSub status for peer: {peer_id}")
            
            # Check if peer is in pubsub.peers
            pubsub_peers = list(self.pubsub.peers.keys())
            logger.info(f"📡 Total PubSub peers: {len(pubsub_peers)}")
            for i, p in enumerate(pubsub_peers, 1):
                logger.info(f"  PubSub peer {i}: {p}")
            
            if peer_id in self.pubsub.peers:
                logger.info(f"✅ Peer {peer_id} is in PubSub mesh")
                
                # Check GossipSub specific status
                if hasattr(self.pubsub, 'router') and hasattr(self.pubsub.router, 'mesh'):
                    mesh = self.pubsub.router.mesh
                    logger.info(f"🕸️  GossipSub mesh status:")
                    logger.info(f"    Mesh topics: {list(mesh.keys())}")
                    for topic, topic_peers in mesh.items():
                        logger.info(f"    Topic '{topic}': {len(topic_peers)} peers")
                        if peer_id in topic_peers:
                            logger.info(f"    ✅ Peer {peer_id} is in mesh for topic '{topic}'")
                        else:
                            logger.warning(f"    ❌ Peer {peer_id} is NOT in mesh for topic '{topic}'")
            else:
                logger.warning(f"❌ Peer {peer_id} is NOT in PubSub mesh")
                logger.info("🔧 Possible reasons:")
                logger.info("  1. PubSub protocol negotiation failed")
                logger.info("  2. Peer doesn't support compatible GossipSub version")
                logger.info("  3. Network issues preventing PubSub handshake")
                
        except Exception as e:
            logger.error(f"❌ Error checking PubSub status: {e}")
    
    async def _setup_chat_room(self):
        """Setup the chat room."""
        logger.info("Setting up chat room...")
        
        self.chat_room = await ChatRoom.join_chat_room(
            host=self.host,
            pubsub=self.pubsub,
            nickname=self.nickname,
            multiaddr=self.full_multiaddr,
            headless_service=self,
            topic=self.topic
        )
        
        # Add custom message handler to forward messages to UI
        self.chat_room.add_message_handler(self._handle_chat_message)
        
        # Start message handlers
        self.running = True
        
        logger.info(f"Chat room setup complete for '{self.nickname}'")
        await self._send_system_message(f"Joined chat room as '{self.nickname}'")
    
    async def _handle_chat_message(self, message: ChatMessage):
        """Handle incoming chat messages and store them per-topic."""
        try:
            topic = message.topic or "default"
            
            # Our own messages are echoed back by GossipSub. They must not show up
            # as unread, or every message you send increments your own badge.
            is_own_message = bool(self.host) and message.sender_id == str(self.host.get_id())
            
            # Check if this is a file sharing message
            is_file_message = message.message.startswith(FILE_MESSAGE_PREFIX)
            
            if is_file_message:
                try:
                    file_meta_json = message.message[len(FILE_MESSAGE_PREFIX):]
                    file_meta = json.loads(file_meta_json)
                    
                    message_data = {
                        'type': 'file_message',
                        'message': message.message,
                        'sender_nick': message.sender_nick,
                        'sender_id': message.sender_id,
                        'timestamp': message.timestamp,
                        'topic': topic,
                        'read': is_own_message,
                        'file_cid': file_meta.get('cid', ''),
                        'file_name': file_meta.get('filename', 'unknown'),
                        'file_size': file_meta.get('filesize', 0),
                    }
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"Failed to parse file message: {e}")
                    # Fall back to regular message
                    message_data = {
                        'type': 'chat_message',
                        'message': message.message,
                        'sender_nick': message.sender_nick,
                        'sender_id': message.sender_id,
                        'timestamp': message.timestamp,
                        'topic': topic,
                        'read': is_own_message
                    }
            else:
                # Store message with unread flag
                message_data = {
                    'type': 'chat_message',
                    'message': message.message,
                    'sender_nick': message.sender_nick,
                    'sender_id': message.sender_id,
                    'timestamp': message.timestamp,
                    'topic': topic,
                    'read': is_own_message
                }
            
            # Give the message its stable id before it reaches the repository,
            # so the stored row and the payload sent to UI/API agree. The
            # repository rejects an id it already holds: GossipSub can deliver
            # the same frame twice (including our own echo), and showing it
            # twice is the bug this dedupe exists to prevent.
            message_data.setdefault('message_id', message_identity(message_data))
            if not await self._store_message(message_data):
                logger.debug("Dropping duplicate message %s", message_data['message_id'])
                return
            
            # Log in simplified format only if not in UI mode
            if not self.ui_mode:
                if is_file_message:
                    logger.info(f"[{topic}] {message.sender_nick} shared a file: {message_data.get('file_name', 'unknown')}")
                else:
                    logger.info(f"[{topic}] {message.sender_nick}: {message.message}")
            
            # Still put message in the legacy UI queue. The queue is bounded,
            # so an API-only deployment with no legacy consumer cannot grow it
            # without limit.
            self._put_legacy(self.message_queue, message_data)
            self._fanout(self.message_subscribers, message_data)
            self._publish_event("message_received", message_data)
            
        except Exception as e:
            logger.error(f"Error handling chat message: {e}")
            logger.exception("Full traceback:")
    
    async def _store_message(self, message_data: Dict[str, Any]) -> bool:
        """Persist a message, keeping slow disk I/O off the networking loop.

        Durable stores run the repository call on a worker thread. A storage
        failure must not take down the node or hide a live message, so it is
        reported as a system message and delivery continues.
        """
        store = self.message_store
        try:
            if getattr(store, "durable", False):
                return await trio.to_thread.run_sync(
                    functools.partial(
                        store.append, message_data, keep=MAX_TOPIC_MESSAGES
                    )
                )
            return store.append(message_data, keep=MAX_TOPIC_MESSAGES)
        except Exception as exc:
            logger.error(f"Failed to store chat message: {exc}")
            await self._send_system_message("Failed to persist a chat message.")
            return True

    def _publish_event(self, event_type, data: Dict[str, Any]) -> None:
        """Publish an application event without ever blocking the trio loop.

        Imported lazily so ``headless`` has no import-time dependency on the
        service layer. Failures must not affect message delivery.
        """
        bus = getattr(self, "event_bus", None)
        if bus is None:
            return
        try:
            from app.events import Event, EventType

            if isinstance(event_type, str):
                try:
                    event_type = EventType(event_type)
                except ValueError:
                    pass
            bus.publish(Event(event_type, dict(data)))
        except Exception:
            logger.debug("Failed to publish %s event", event_type, exc_info=True)

    async def _send_system_message(self, message: str):
        """Send system message to UI queue."""
        logger.debug(f"_send_system_message called with: {message}")
        try:
            self._publish_event(
                "system",
                {"type": "system_message", "message": message, "timestamp": time.time()},
            )
            if self.system_queue:
                logger.debug(f"System queue available, sending message: {message}")
                payload = {
                    'type': 'system_message',
                    'message': message,
                    'timestamp': time.time()
                }
                self._put_legacy(self.system_queue, payload)
                self._fanout(self.system_subscribers, payload)
                logger.debug(f"System message sent successfully: {message}")
            else:
                logger.warning(f"System queue not available, cannot send message: {message}")
        except Exception as e:
            logger.error(f"Error sending system message: {e}")
            logger.exception("Full traceback:")
    
    async def _process_messages(self):
        """Process messages from chat room."""
        try:
            # Start chat room message handlers
            await self.chat_room.start_message_handlers()
        except Exception as e:
            logger.error(f"Error in message processing: {e}")
    
    async def _process_outgoing_messages(self):
        """Process outgoing messages from UI to chat room."""
        
        while self.running:
            try:
                # Check for messages from UI (non-blocking)
                try:
                    outgoing_data = self.outgoing_queue.sync_q.get_nowait()
                    if outgoing_data and 'message' in outgoing_data:
                        message = outgoing_data['message']
                        topic = outgoing_data.get('topic')  # Optional topic parameter
                        
                        # Send message through chat room
                        if self.chat_room and self.running:
                            if topic:
                                # Send to specific topic
                                success = await self.chat_room.publish_to_topic(topic, message)
                                if not self.ui_mode:
                                    logger.info(f"{self.nickname} (you) to {topic}: {message}")
                            else:
                                # Send to default chat topic
                                await self.chat_room.publish_message(message)
                                if not self.ui_mode:
                                    logger.info(f"{self.nickname} (you): {message}")
                        else:
                            logger.warning("Cannot send message: chat room not ready")
                            await self._send_system_message("Cannot send message: chat room not ready")
                            
                except Empty:
                    # No message available, that's fine
                    await trio.sleep(0.1)  # Brief pause to avoid busy loop
                except Exception as e:
                    logger.error(f"Error processing outgoing message: {e}")
                    await trio.sleep(0.1)
                    
            except Exception as e:
                logger.error(f"Error in outgoing message processing: {e}")
                await trio.sleep(0.1)
    
    async def _process_topic_subscriptions(self):
        """Process topic subscription requests from UI."""
        
        while self.running:
            try:
                # Check for subscription requests from UI (non-blocking)
                try:
                    subscription_data = self.topic_subscription_queue.sync_q.get_nowait()
                    if subscription_data and 'topic' in subscription_data:
                        topic_name = subscription_data['topic']
                        
                        # Subscribe to the topic through chat room
                        if self.chat_room and self.running:
                            success = await self.chat_room.subscribe_to_topic(topic_name)
                            if success:
                                logger.info(f"Successfully subscribed to topic: {topic_name}")
                                self._publish_event("topic_subscribed", {"topic": topic_name})
                                await self._send_system_message(f"Subscribed to topic: {topic_name}")
                            else:
                                logger.warning(f"Failed to subscribe to topic: {topic_name}")
                                await self._send_system_message(f"Failed to subscribe to topic: {topic_name}")
                        else:
                            logger.warning("Cannot subscribe to topic: chat room not ready")
                            await self._send_system_message("Cannot subscribe to topic: chat room not ready")
                            
                except Empty:
                    # No request available, that's fine
                    await trio.sleep(0.1)  # Brief pause to avoid busy loop
                except Exception as e:
                    logger.error(f"Error processing topic subscription: {e}")
                    await trio.sleep(0.1)
                    
            except Exception as e:
                logger.error(f"Error in topic subscription processing: {e}")
                await trio.sleep(0.1)
    
    async def _process_peer_connections(self):
        """Process peer connection requests from UI."""
        
        while self.running:
            try:
                # Check for connection requests from UI (non-blocking)
                try:
                    multiaddr_str = self.peer_connection_queue.sync_q.get_nowait()
                    if multiaddr_str:
                        logger.info(f"Processing peer connection request: {multiaddr_str}")
                        
                        # Parse and connect to the peer
                        try:
                            # Parse the multiaddress
                            maddr = multiaddr.Multiaddr(self._normalize_dial_addr(multiaddr_str))
                            
                            # Try to get peer info from the multiaddress
                            peer_info = info_from_p2p_addr(maddr)
                            
                            if peer_info:
                                # Connect to the peer
                                logger.info(f"Attempting to connect to peer: {peer_info.peer_id}")
                                await self.host.connect(peer_info)
                                logger.info(f"✅ Successfully connected to peer: {peer_info.peer_id}")
                                await self._send_system_message(f"Connected to peer: {peer_info.peer_id}")
                            else:
                                logger.error(f"Could not extract peer info from multiaddress: {multiaddr_str}")
                                await self._send_system_message(f"Invalid multiaddress format")
                                
                        except Exception as e:
                            logger.error(f"Failed to connect to peer {multiaddr_str}: {e}")
                            await self._send_system_message(f"Connection failed: {str(e)}")
                            
                except Empty:
                    # No request available, that's fine
                    await trio.sleep(0.1)  # Brief pause to avoid busy loop
                except Exception as e:
                    logger.error(f"Error processing peer connection: {e}")
                    await trio.sleep(0.1)
                    
            except Exception as e:
                logger.error(f"Error in peer connection processing: {e}")
                await trio.sleep(0.1)

    async def _process_file_shares(self):
        """Process file sharing requests from UI."""
        while self.running:
            try:
                try:
                    share_data = self.file_share_queue.sync_q.get_nowait()
                    if share_data:
                        file_path = share_data.get('file_path')
                        topic = share_data.get('topic')
                        
                        if not file_path or not os.path.exists(file_path):
                            logger.error(f"File not found: {file_path}")
                            await self._send_system_message(f"File not found: {file_path}")
                            continue
                        
                        filename = os.path.basename(file_path)
                        filesize = os.path.getsize(file_path)
                        
                        logger.info(f"📁 Sharing file: {filename} ({filesize} bytes)")
                        await self._send_system_message(f"Preparing to share: {filename}...")
                        
                        try:
                            # Add file to Merkle DAG (chunks + stores in bitswap)
                            root_cid = await self.merkle_dag.add_file(
                                file_path,
                                wrap_with_directory=True
                            )
                            
                            cid_hex = root_cid.hex()
                            
                            # Track shared file locally. filepath stays
                            # server-side; the API only returns filename/size.
                            with self._state_lock:
                                self.shared_files[cid_hex] = {
                                    'filename': filename,
                                    'filesize': filesize,
                                    'filepath': file_path,
                                }
                            
                            logger.info(f"✅ File added to DAG. CID: {cid_hex}")
                            
                            # Create file sharing message with metadata
                            file_meta = {
                                'cid': cid_hex,
                                'filename': filename,
                                'filesize': filesize,
                            }
                            file_message = f"{FILE_MESSAGE_PREFIX}{json.dumps(file_meta)}"
                            
                            # Publish file message via pubsub to the topic
                            if topic and self.chat_room:
                                success = await self.chat_room.publish_to_topic(topic, file_message)
                                if success:
                                    logger.info(f"✅ File shared to topic '{topic}': {filename}")
                                    
                                    # Don't store the message here - the pubsub echo
                                    # will come back through _handle_chat_message and store it.
                                    # Only notify the UI immediately so it shows the bubble.
                                    payload = {
                                        'type': 'file_shared',
                                        'topic': topic,
                                        'file_cid': cid_hex,
                                        'file_name': filename,
                                        'file_size': filesize,
                                        'sender_nick': 'You',
                                        'sender_id': 'self',
                                        'timestamp': time.time(),
                                    }
                                    self._put_legacy(self.message_queue, payload)
                                    self._fanout(self.message_subscribers, payload)
                                else:
                                    await self._send_system_message(f"Failed to share file to topic")
                            else:
                                await self._send_system_message(f"No topic specified for file sharing")
                                
                        except Exception as e:
                            logger.error(f"Failed to share file: {e}")
                            logger.exception("Full traceback:")
                            await self._send_system_message(f"Failed to share file: {str(e)}")
                            
                except Empty:
                    await trio.sleep(0.1)
                except Exception as e:
                    logger.error(f"Error processing file share: {e}")
                    await trio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error in file share processing: {e}")
                await trio.sleep(0.1)

    async def _process_file_downloads(self):
        """Process file download requests from UI."""
        while self.running:
            try:
                try:
                    download_data = self.file_download_queue.sync_q.get_nowait()
                    if download_data:
                        cid_hex = download_data.get('file_cid')
                        filename = download_data.get('file_name', 'unknown')
                        
                        if not cid_hex:
                            logger.error("No CID provided for download")
                            continue
                        
                        logger.info(f"📥 Downloading file: {filename} (CID: {cid_hex})")
                        await self._send_system_message(f"Downloading: {filename}...")
                        
                        try:
                            root_cid = bytes.fromhex(cid_hex)
                            
                            # Fetch file via bitswap
                            file_data, extracted_filename = await self.merkle_dag.fetch_file(
                                root_cid,
                                timeout=60.0
                            )
                            
                            # Use extracted filename if available, fallback to provided
                            # name. Both are attacker-controlled (the filename travels
                            # inside the pubsub file message), so strip path
                            # components before joining.
                            save_filename = os.path.basename(extracted_filename or filename).strip()
                            if save_filename in ("", ".", ".."):
                                save_filename = "download"
                            
                            # Ensure download directory exists
                            os.makedirs(self.download_dir, exist_ok=True)
                            
                            # Handle filename conflicts
                            save_path = os.path.join(self.download_dir, save_filename)
                            download_root = os.path.realpath(self.download_dir)
                            if os.path.commonpath([download_root, os.path.realpath(save_path)]) != download_root:
                                raise ValueError("Refusing to write outside the download directory")
                            if os.path.exists(save_path):
                                name, ext = os.path.splitext(save_filename)
                                counter = 1
                                while os.path.exists(save_path):
                                    save_path = os.path.join(self.download_dir, f"{name}_{counter}{ext}")
                                    counter += 1
                            
                            # Write file off the trio event loop: a large
                            # download must not stall pubsub/DHT/heartbeats.
                            await trio.to_thread.run_sync(
                                _write_bytes, save_path, file_data
                            )
                            
                            logger.info(f"✅ File downloaded: {save_path} ({len(file_data)} bytes)")
                            
                            # Notify UI
                            payload = {
                                'type': 'file_downloaded',
                                'file_cid': cid_hex,
                                'file_name': save_filename,
                                'file_size': len(file_data),
                                'save_path': save_path,
                                'timestamp': time.time(),
                            }
                            self._put_legacy(self.message_queue, payload)
                            self._fanout(self.message_subscribers, payload)
                            
                        except Exception as e:
                            logger.error(f"Failed to download file: {e}")
                            logger.exception("Full traceback:")
                            payload = {
                                'type': 'file_download_failed',
                                'file_cid': cid_hex,
                                'file_name': filename,
                                'error': str(e),
                                'timestamp': time.time(),
                            }
                            self._put_legacy(self.message_queue, payload)
                            self._fanout(self.message_subscribers, payload)
                            
                except Empty:
                    await trio.sleep(0.1)
                except Exception as e:
                    logger.error(f"Error processing file download: {e}")
                    await trio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error in file download processing: {e}")
                await trio.sleep(0.1)

    async def _wait_for_stop(self):
        """Poll the thread-safe stop signal without blocking the trio thread."""
        while not self.stop_event.is_set():
            await trio.sleep(0.1)
        logger.info("Stop signal received, shutting down...")
        self.running = False
    
    def send_message(self, message: str) -> bool:
        """Queue a message for the default chat topic (thread-safe)."""
        if self.outgoing_queue and self.running:
            try:
                # Non-blocking: this is called from the Tornado/UI thread and
                # must never stall the API event loop if the publisher is behind.
                self.outgoing_queue.sync_q.put_nowait({
                    'message': message,
                    'timestamp': time.time()
                })
                return True
            except Full:
                logger.warning("Outgoing message queue is full; dropping message")
            except Exception as e:
                logger.error(f"Failed to queue message: {e}")
        else:
            logger.warning("Cannot send message: outgoing queue not ready or service not running")
        return False
    
    def send_message_to_topic(self, topic: str, message: str) -> bool:
        """Queue a message for a specific topic (thread-safe)."""
        if self.outgoing_queue and self.running:
            try:
                self.outgoing_queue.sync_q.put_nowait({
                    'message': message,
                    'topic': topic,
                    'timestamp': time.time()
                })
                return True
            except Full:
                logger.warning(
                    f"Outgoing message queue is full; dropping message for {topic}"
                )
            except Exception as e:
                logger.error(f"Failed to queue message to topic {topic}: {e}")
        else:
            logger.warning("Cannot send message: outgoing queue not ready or service not running")
        return False
    
    def get_connection_info(self) -> Dict[str, Any]:
        """Get connection information for UI."""
        if not self.ready:
            return {}
        
        return {
            'peer_id': str(self.host.get_id()),
            'nickname': self.nickname,
            'multiaddr': self.full_multiaddr,
            'multiaddrs': list(self.full_multiaddrs),
            'listen_addrs': list(self.transport_addrs),
            'connected_peers': self.chat_room.get_connected_peers() if self.chat_room else set(),
            'peer_count': self.chat_room.get_peer_count() if self.chat_room else 0
        }
    
    def get_subscribed_topics(self) -> Set[str]:
        """Get user-facing chat subscriptions (protocol topics excluded)."""
        if not self.chat_room:
            return set()
        return self.chat_room.get_subscribed_topics()

    def get_all_pubsub_topics(self) -> Set[str]:
        """Get every pubsub subscription, including protocol-only topics."""
        if not self.chat_room:
            return set()
        return self.chat_room.get_all_subscriptions()

    def get_shared_files(self) -> List[Dict[str, Any]]:
        """Snapshot shared-file metadata without exposing local paths."""
        with self._state_lock:
            return [
                {
                    "cid": cid,
                    "filename": meta.get("filename"),
                    "filesize": meta.get("filesize"),
                }
                for cid, meta in self.shared_files.items()
            ]

    def get_shared_file(self, cid: str):
        """Return one shared-file metadata entry, or None."""
        with self._state_lock:
            meta = self.shared_files.get(cid)
            if not meta:
                return None
            return {
                "cid": cid,
                "filename": meta.get("filename"),
                "filesize": meta.get("filesize"),
            }

    def get_peer_info_cache_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot the identify cache for API readout."""
        with self._state_lock:
            return {
                peer_id: dict(info)
                for peer_id, info in self.peer_info_cache.items()
            }

    def get_peer_info(self, peer_id: str):
        """Return one cached identify entry, or None."""
        with self._state_lock:
            info = self.peer_info_cache.get(peer_id)
            return dict(info) if info else None

    def delete_peer_info(self, peer_id: str) -> bool:
        """Invalidate one cached identify entry."""
        with self._state_lock:
            return self.peer_info_cache.pop(peer_id, None) is not None

    def get_pubsub_peers(self) -> List[str]:
        """Snapshot pubsub peers, retrying if the live dict changes mid-copy."""
        if not self.pubsub:
            return []
        for _ in range(3):
            try:
                return [str(p) for p in self.pubsub.peers.keys()]
            except RuntimeError:
                continue
        return []

    def get_gossipsub_mesh(self) -> Dict[str, List[str]]:
        """Snapshot GossipSub mesh, retrying on concurrent modification."""
        if not self.gossipsub:
            return {}
        mesh = getattr(self.gossipsub, "mesh", {})
        for _ in range(3):
            try:
                return {
                    topic: [str(p) for p in peers]
                    for topic, peers in mesh.items()
                }
            except RuntimeError:
                continue
        return {}

    def get_gossipsub_fanout(self) -> Dict[str, List[str]]:
        """Snapshot GossipSub fanout, retrying on concurrent modification."""
        if not self.gossipsub:
            return {}
        fanout = getattr(self.gossipsub, "fanout", {})
        for _ in range(3):
            try:
                return {
                    topic: [str(p) for p in peers]
                    for topic, peers in fanout.items()
                }
            except RuntimeError:
                continue
        return {}
    
    def subscribe_to_topic(self, topic_name: str) -> bool:
        """
        Subscribe to a new topic (thread-safe wrapper).
        
        Args:
            topic_name: The name of the topic to subscribe to
            
        Returns:
            True if subscription request was queued, False otherwise
        """
        if not self.chat_room or not self.running:
            logger.warning("Cannot subscribe to topic: chat room not ready or service not running")
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
        if len(self.get_subscribed_topics()) >= MAX_TOPICS:
            logger.warning(f"Cannot subscribe to topic: limit {MAX_TOPICS} reached")
            return False
        
        try:
            # Put subscription request in queue (sync call, safe from UI thread)
            self.topic_subscription_queue.sync_q.put_nowait({
                'topic': topic_name,
                'timestamp': time.time()
            })
            logger.info(f"Queued subscription request for topic: {topic_name}")
            return True
        except Full:
            logger.warning("Topic subscription queue is full; dropping request")
            return False
        except Exception as e:
            logger.error(f"Failed to queue topic subscription: {e}")
            return False
    
    def connect_to_peer(self, multiaddr: str) -> bool:
        """
        Connect to a peer using multiaddress (thread-safe wrapper).
        
        Args:
            multiaddr: The multiaddress of the peer to connect to
            
        Returns:
            True if connection request was queued, False otherwise
        """
        if not self.host or not self.running:
            logger.warning("Cannot connect to peer: host not ready or service not running")
            return False
        
        try:
            # 0.0.0.0 is a bind address, not a dial address — rewrite to loopback
            multiaddr = self._normalize_dial_addr(multiaddr)
            # Put connection request in queue (sync call, safe from UI thread)
            self.peer_connection_queue.sync_q.put_nowait(multiaddr)
            logger.info(f"Queued peer connection request: {multiaddr}")
            return True
        except Full:
            logger.warning("Peer connection queue is full; dropping request")
            return False
        except Exception as e:
            logger.error(f"Failed to queue peer connection: {e}")
            return False
    
    def share_file(self, file_path: str, topic: str) -> bool:
        """
        Share a file to a topic via bitswap (thread-safe wrapper).
        
        Args:
            file_path: Path to the file to share
            topic: The topic to share the file in
            
        Returns:
            True if file share request was queued, False otherwise
        """
        if not self.running or not self.file_share_queue:
            logger.warning("Cannot share file: service not ready")
            return False
        
        try:
            self.file_share_queue.sync_q.put_nowait({
                'file_path': file_path,
                'topic': topic,
                'timestamp': time.time(),
            })
            logger.info(f"Queued file share request: {file_path} -> {topic}")
            return True
        except Full:
            logger.warning("File share queue is full; dropping request")
            return False
        except Exception as e:
            logger.error(f"Failed to queue file share: {e}")
            return False
    
    def download_file(self, file_cid: str, file_name: str = "unknown") -> bool:
        """
        Download a file by CID via bitswap (thread-safe wrapper).
        
        Args:
            file_cid: Hex CID of the file to download
            file_name: Expected filename
            
        Returns:
            True if download request was queued, False otherwise
        """
        if not self.running or not self.file_download_queue:
            logger.warning("Cannot download file: service not ready")
            return False
        
        try:
            self.file_download_queue.sync_q.put_nowait({
                'file_cid': file_cid,
                'file_name': file_name,
                'timestamp': time.time(),
            })
            logger.info(
                f"Queued file download request: {file_name} "
                f"(CID: {str(file_cid)[:16]}...)"
            )
            return True
        except Full:
            logger.warning("File download queue is full; dropping request")
            return False
        except Exception as e:
            logger.error(f"Failed to queue file download: {e}")
            return False
    
    def register_subscriber(self, kind: str = "message"):
        """Register a per-WebSocket-client queue that receives every event."""
        subscribers = self.message_subscribers if kind == "message" else self.system_subscribers
        queue = janus.Queue(maxsize=256)
        with self._subscribers_lock:
            subscribers.add(queue)
        return queue

    def unregister_subscriber(self, kind: str, queue) -> None:
        """Remove and close a previously registered subscriber queue."""
        subscribers = self.message_subscribers if kind == "message" else self.system_subscribers
        with self._subscribers_lock:
            subscribers.discard(queue)
        try:
            queue.close()
        except Exception:
            pass

    @staticmethod
    def _put_legacy(queue, payload) -> None:
        """
        Best-effort write to a bounded legacy UI queue.

        A legacy queue is optional (API/headless deployments have no consumer),
        so a full or closed queue must never block the trio thread or raise.
        """
        if queue is None:
            return
        try:
            queue.sync_q.put_nowait(payload)
        except Exception:
            pass

    def _fanout(self, subscribers, payload) -> None:
        """Copy an event to every registered subscriber (non-blocking)."""
        # Snapshot under the lock: WebSocket open/close happens on the Tornado
        # thread while _fanout runs on the trio thread. Iterating the live set
        # can raise "Set changed size during iteration" and drop the event.
        with self._subscribers_lock:
            snapshot = list(subscribers)
        for queue in snapshot:
            try:
                # sync_q is the thread-safe side: producers run on the trio
                # thread, consumers on the Tornado/asyncio thread.
                queue.sync_q.put_nowait(payload)
            except Exception:
                # Queue full (slow client) or closed — drop this client's copy.
                pass

    def get_message_queue(self):
        """Get the message queue for UI."""
        return self.message_queue
    
    def get_system_queue(self):
        """Get the system queue for UI."""
        return self.system_queue
    
    def get_topic_messages(self, topic: str) -> List[Dict[str, Any]]:
        """
        Get all messages for a specific topic.
        
        Args:
            topic: The topic name
            
        Returns:
            List of message dictionaries (a snapshot, safe to serialize while
            the trio thread appends to the live history).
        """
        messages, _total = self.message_store.page(
            topic, limit=min(MAX_TOPIC_MESSAGES, 1000), offset=0
        )
        return messages
    
    def get_all_topics_with_info(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all subscribed topics with their message counts and unread status.
        
        Returns:
            Dict mapping topic names to info dicts containing:
            - unread_count: Number of unread messages
            - total_count: Total number of messages
            - last_message: Most recent message (if any)
        """
        return {
            topic: self.message_store.stats(topic)
            for topic in self.get_subscribed_topics()
        }
    
    def mark_topic_as_read(self, topic: str):
        """
        Mark all messages in a topic as read.
        
        Args:
            topic: The topic name
        """
        marked = self.message_store.mark_read(topic)
        if marked:
            logger.debug(f"Marked {marked} messages in topic '{topic}' as read")
    
    def get_unread_count(self, topic: str) -> int:
        """
        Get the count of unread messages for a topic.
        
        Args:
            topic: The topic name
            
        Returns:
            Number of unread messages
        """
        return self.message_store.unread_count(topic)
    
    def get_outgoing_queue(self):
        """Get the outgoing queue for UI to send messages."""
        return self.outgoing_queue
    
    async def get_peer_info_via_identify(self, peer_id):
        """Get peer information using official identify protocol implementation."""
        try:
            logger.info(f"🔍 Requesting identify info from peer: {peer_id}")
            logger.info(f"peers in peer store are: {self.host.get_peerstore().peers_with_addrs()}")
            logger.info(f"address of peer {peer_id} is {self.host.get_peerstore().peer_info(peer_id).addrs} ")
            
            # Create a stream to the peer for identify protocol - use tuple format as in example
            stream = await self.host.new_stream(peer_id, (IDENTIFY_PROTOCOL_ID,))
            
            try:
                # Use official py-libp2p utilities to read the response
                # Use raw protobuf format (use_varint_format=False) for go-libp2p compatibility
                # go-libp2p uses the old/raw format, not the newer varint length-prefixed format
                response_bytes = await read_length_prefixed_protobuf(stream, use_varint_format=True)
                
                if not response_bytes:
                    logger.warning(f"Empty identify response from peer: {peer_id}")
                    return None
                
                # Parse the identify response using official parser
                identify_info = parse_identify_response(response_bytes)
                
                logger.info(f"✅ Received identify info from {peer_id}")
                logger.info(f"  - Protocol Version: {identify_info.protocol_version}")
                logger.info(f"  - Agent Version: {identify_info.agent_version}")
                logger.info(f"  - Public Key: {len(identify_info.public_key)} bytes")
                logger.info(f"  - Listen Addresses: {len(identify_info.listen_addrs)} addresses")
                logger.info(f"  - Protocols: {len(identify_info.protocols)} protocols")
                
                # Store the peer info in our cache
                with self._state_lock:
                    self.peer_info_cache[str(peer_id)] = {
                        'public_key': identify_info.public_key,
                        'protocol_version': identify_info.protocol_version,
                        'agent_version': identify_info.agent_version,
                        'listen_addrs': identify_info.listen_addrs,
                        'protocols': identify_info.protocols,
                        'timestamp': time.time()
                    }
                
                return identify_info
                
            finally:
                await stream.close()
                
        except Exception as e:
            logger.error(f"❌ Failed to get identify info from peer {peer_id}: {e}")
            return None
    
    async def get_cached_peer_info(self, peer_id: str):
        """Get cached peer info, or fetch it if not available."""
        peer_id_str = str(peer_id)
        
        # Check if we have cached info
        with self._state_lock:
            cached_info = self.peer_info_cache.get(peer_id_str)
        # Check if cache is not too old (5 minutes)
        if cached_info and time.time() - cached_info['timestamp'] < 300:
            return dict(cached_info)
        elif cached_info:
            logger.debug(f"Cached info for {peer_id_str} is stale, refreshing")
        
        # Fetch fresh info
        try:
            peer_id_obj = ID.from_base58(peer_id_str) if isinstance(peer_id, str) else peer_id
            identify_info = await self.get_peer_info_via_identify(peer_id_obj)
            
            if identify_info:
                with self._state_lock:
                    info = self.peer_info_cache.get(peer_id_str)
                    return dict(info) if info else None
        except Exception as e:
            logger.error(f"❌ Failed to get peer info for {peer_id_str}: {e}")
        
        return None

    def get_public_key_for_peer(self, peer_id: str):
        """Get public key for a peer (synchronous access to cache)."""
        peer_id_str = str(peer_id)
        with self._state_lock:
            info = self.peer_info_cache.get(peer_id_str)
            return info.get('public_key') if info else None
    
    def request_stop(self):
        """Ask the service to stop. Safe to call from any thread.

        This only signals intent. The trio thread that owns the host performs
        the actual teardown once, in ``start()``'s ``finally`` block. Signal
        handlers, UI threads, and Tornado handlers should use this method
        instead of mutating ``stop_event`` directly.
        """
        self.stop_event.set()

    async def _shutdown_async(self):
        """Tear down resources owned by the trio loop. Runs at most once."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("Stopping headless service...")

        if self.chat_room:
            try:
                await self.chat_room.stop()
            except Exception:
                logger.exception("Error stopping ChatRoom")

        # libp2p's Pubsub allocates a ``LastSeenCache`` whose daemon cleanup
        # thread is not joined by the service context manager. Leaving it alive
        # leaks one thread per start/stop cycle in long-lived embedders.
        pubsub = getattr(self, "pubsub", None)
        seen_messages = getattr(pubsub, "seen_messages", None)
        stop_cache = getattr(seen_messages, "stop", None)
        if stop_cache is not None:
            try:
                stop_cache()
            except Exception:
                logger.exception("Error stopping the pubsub seen-message cache")

        # Stop bitswap client
        if self.bitswap_client:
            try:
                await self.bitswap_client.stop()
            except Exception:
                logger.exception("Error stopping Bitswap client")

        # Close per-client subscriber queues
        with self._subscribers_lock:
            subscriber_queues = list(
                self.message_subscribers | self.system_subscribers
            )
            self.message_subscribers.clear()
            self.system_subscribers.clear()
        for queue in subscriber_queues:
            try:
                queue.close()
            except Exception:
                pass

        # Close the legacy queues. close() is best-effort: a queue may never
        # have been created (early startup failure) or already be closed.
        for queue in (
            self.message_queue,
            self.system_queue,
            self.outgoing_queue,
            self.topic_subscription_queue,
            self.peer_connection_queue,
            self.file_share_queue,
            self.file_download_queue,
        ):
            if queue:
                try:
                    queue.close()
                except Exception:
                    pass

        logger.info("Headless service stopped")

    async def stop(self):
        """Request shutdown.

        Kept async for existing callers running inside the trio loop. The
        actual teardown is performed once by the owning trio thread in
        ``start()``; this method never blocks waiting for that teardown, so it
        is safe to call from a task owned by ``start()``.
        """
        self.request_stop()
