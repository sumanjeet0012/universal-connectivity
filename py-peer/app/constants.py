"""Application-wide constants shared by the engine and the service layer.

Keeping these values here means services no longer import ``headless`` (the
transport engine) just to read a limit or a bootstrap address.
"""

from __future__ import annotations

import os

# Bootstrap nodes for peer discovery.
BOOTSTRAP_PEERS = [
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmNnooDu7bfjPFoTZYxMNLWUQJyrVwtbZg5gBMjTezGAJN",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmQCU2EcMqAqQPR2i9bChDtGNJchTbq5TbXJJ16u19uLTa",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmbLHAnMoJPWSCR5Zp7ykQCj2gRNdrFeqQ1vG13rMb4sPS",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmcZf59bWwK5XFi76CZX8cbJ4BhTzzA3gU1ZjYZcYW3dwt",
    "/ip4/104.131.131.82/tcp/4001/p2p/QmaCpDMGvV2BGHeYERUEnRQAwe3N8SzbUtfsmvsqQLuvuJ",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmbLHAnMoJPWSCR5Zhtx6BHJX9KiKNN6tpvbUcqanj75Nb",
]

# Prefix used to mark a chat message as a file-share announcement.
FILE_MESSAGE_PREFIX = "[FILE]"

# Default download directory for shared/downloaded files.
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

# Resource bounds. The API and the network are untrusted inputs: without caps
# a slow client, a busy topic, or a message flood can grow these structures
# until the peer is OOM-killed.
MAX_TOPIC_MESSAGES = 1000        # stored history per topic
MAX_LEGACY_QUEUE = 1024          # UI compatibility queue
MAX_OUTGOING_QUEUE = 1024        # API/UI -> chat publisher
MAX_REQUEST_QUEUE = 256          # topic/peer requests
MAX_FILE_QUEUE = 64              # file share/download requests

# Discovery service tag and default listener port.
DISCOVERY_SERVICE_TAG = "universal-connectivity"
DEFAULT_PORT = 9095
