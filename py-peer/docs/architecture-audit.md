# Universal Connectivity py-peer — Architecture Audit (Phase 0)

**Status:** Phase 0 deliverable  
**Audit date:** 2026-11-08  
**Repository:** `universal-connectivity` (`py-peer` is the subject of this audit)  
**Baseline revision:** `dcfbf07` plus the current uncommitted working tree  
**Auditor method:** source inspection, dependency inspection, package build, test/lint/build execution

> This document records the **current** system, not the target architecture in the
> implementation brief. Every claim below is marked as **Verified**, **Assumption**,
> or **Unknown** unless it is directly quoted from source or command output.
>
> The working tree is dirty. Several files (`main.py`, `headless.py`, `api/*`,
> `pyproject.toml`, the React frontend) already contain uncommitted changes, and
> `tests/`, `system_messages.txt`, and `.venv/` are untracked. Baseline results
> therefore describe the working tree, not commit `dcfbf07` alone.

---

## 1. Executive summary

The Python peer is a single-process application with a usable
`HeadlessService`, a Tornado REST/WebSocket API, a Textual TUI, a Kivy UI, a
simple CLI, and a separate React frontend. It works, but it does not yet meet the
implementation brief's architecture goals.

The most important verified findings are:

1. **One host is created per running `HeadlessService`**, in
   `headless.py:_run_service` (`headless.py:309`). No UI or API module creates a
   libp2p host. API mode and TUI mode are mutually exclusive CLI branches that
   each construct one `HeadlessService`.
2. **Only TCP/IPv4 is configured.** `new_host()` is called without QUIC,
   WebSocket, WebRTC, WebTransport, mDNS, UPnP, AutoTLS, or relay options. The
   installed libp2p 0.8.1 supports some of these behind flags, but this app does
   not enable them.
3. **The advertised address is built by hand and defaults to loopback**
   (`/ip4/127.0.0.1/tcp/<port>/p2p/<peer-id>`). It is not derived from the
   listener or from identify-observed addresses. Remote peers cannot dial the
   default announcement.
4. **No durable application storage exists.** Messages, unread counts, peer
   identify metadata, shared-file metadata, and Bitswap blocks are in memory.
   Identity is re-derived from a seed on every launch, and the default seed
   (`"py-peer"`) is shared by every peer that does not override it.
5. **Writable state depends on the current working directory.** The rotating
   `system_messages.txt` log is created relative to CWD at import time, `.env` is
   loaded from CWD, and downloads go to `~/Downloads`. There is no
   `UC_DATA_DIR`, no `platformdirs` usage in application code, and no
   application data directory.
6. **Kivy is a mandatory dependency** even though it is only imported by
   `kivy_ui.py` (deferred import in `main.py`). No Android/buildozer/
   python-for-android files exist, and no py-peer CI workflow exists.
7. **The package build is not distributable as-is:** `uv build` succeeds, but the
   wheel is **36 MB / 129 MB uncompressed** and contains **13,572
   `py-peer-frontend/node_modules` entries**, plus `dist/`, tests, and docs. The
   wheel metadata does not preserve the fork of py-libp2p used by `uv.lock`.
8. **Baseline tests pass** (25/25), but Python lint fails with 83 ruff errors,
   there are no host/transport/persistence/API integration tests, and the tests
   are untracked in git.
9. **The API is unauthenticated, CORS-open, and bound to all interfaces** when
   `--api` is used. This is acceptable only for trusted local development; it is
   not a safe default for remote exposure.

Phase 0 is complete enough to proceed, but two decisions must be made before
durable identity work: how to migrate from seeded identities to persisted keys,
and what the supported deployment/network model is (loopback dev vs LAN vs
Docker/remote).

---

## 2. Repository inventory

### 2.1 Python package and entry points

| Item | Verified fact | Evidence |
|---|---|---|
| Package root | `py-peer/` | repository tree |
| Import package name | None. Top-level modules (`main`, `headless`, `chatroom`, `api`, …) are imported directly. | `pyproject.toml`, imports |
| Build backend | Hatchling | `pyproject.toml:25-30` |
| Wheel packaging | `packages = ["."]` (packages the whole directory) | `pyproject.toml:29-30` |
| Console script | `py-peer = main:main` | `pyproject.toml:22-23` |
| Python requirement | `>=3.12` | `pyproject.toml:6` |
| Version | `0.1.0` | `pyproject.toml:2` |

Entry points are all in `main.py:main()`:

| Mode | CLI flag | What runs |
|---|---|---|
| API | `--api [--api-port N]` | `HeadlessService` in a daemon trio thread + Tornado in the main thread |
| Kivy UI | `--kivy` | `HeadlessService` in a daemon trio thread + Kivy in the main thread |
| Textual TUI | `--ui` | `HeadlessService` in a daemon trio thread + Textual in the main thread |
| Simple CLI | no mode flag | `trio.run(main_async)` with stdin/stdout chat |
| Headless | `--headless` | `trio.run(main_async)` → `await service.start()` |
| Route listing | `--api-routes` | prints routes and exits |

### 2.2 Major modules

| Path | Lines | Responsibility |
|---|---:|---|
| `main.py` | 504 | Argument parsing, mode selection, headless-thread bootstrap |
| `headless.py` | 1539 | `HeadlessService`: host, DHT, GossipSub/Pubsub, Bitswap, queues, in-memory state, API-facing methods |
| `chatroom.py` | 513 | Topic subscriptions, message decode/forwarding, peer snapshots |
| `tornado_server.py` | 229 | Tornado application and route table |
| `api/*.py` | ~1,300 total | REST/WebSocket adapters over `HeadlessService` |
| `ui.py` | 323 | Textual TUI; reads janus queues, calls service directly |
| `kivy_ui.py` | 1374 | Kivy/KivyMD UI; reads queues, calls service directly |
| `rag_handler.py` | 178 | Optional ChromaDB + Groq `/api/v1/ask` endpoint |
| `py-peer-frontend/` | — | React + Vite + TypeScript client |
| `tests/` | 6 files, 25 tests | `unittest` unit/regression tests (untracked) |

### 2.3 Current architecture (verified)

```text
                         Entry: main.py / py-peer console script
                                        |
        +-------------------+-----------+-----------+-------------------+
        |                   |                       |                   |
        v                   v                       v                   v
   Simple CLI          Textual TUI              Kivy UI            API mode
 (same trio run)    (thread boundary)        (thread boundary)   (thread boundary)
        |                   |                       |                   |
        |        direct in-process calls + janus queues                 |
        +-------------------+-----------+-----------+-------------------+
                                        |
                                        v
                              HeadlessService
                +-----------------------------------------------+
                | queues, topic history, unread counts,          |
                | shared-file metadata, identify cache,          |
                | stop_event/ready flags, locking                |
                +-----------------------------------------------+
                                        |
                                        v
                     one libp2p host (created in _run_service)
                +-----------------------------------------------+
                | TCP/IPv4 transport (0.0.0.0:port)              |
                | identify handler                               |
                | KadDHT (SERVER, random walk)                   |
                | GossipSub + Pubsub (v1.0/v1.1/v1.2)            |
                | Bitswap + MerkleDag (MemoryBlockStore)         |
                | BootstrapDiscovery                             |
                +-----------------------------------------------+
                                        |
                                        v
                     ALL APPLICATION DATA IN PROCESS MEMORY
                     (no database, no identity file, no message store)

  React frontend (separate process)
        |  HTTP /api/v1/* + WS /ws/*
        v
  TornadoServer ---- adapters ----^  (same HeadlessService object)
```

### 2.4 What does *not* exist

Verified absent in the py-peer working tree:

- Dockerfile, Docker Compose file, `.dockerignore` for py-peer.
- Buildozer/spec/python-for-android/Android packaging files.
- `docs/` directory (this audit creates it).
- Database, ORM, migration framework, or durable key store.
- libp2p ping handler, mDNS, UPnP, AutoNAT, circuit relay, QUIC, WebSocket
  transport, WebRTC, WebTransport, IPv6 listener.
- Authentication/authorization for the HTTP API.
- Python type-check configuration (mypy/pyright) and ruff configuration.
- CI workflow for py-peer.
- Automated transport, host-lifecycle, persistence, or API endpoint tests.

---

## 3. Startup and shutdown audit

### 3.1 API mode (`--api`)

Verified sequence:

1. `main()` parses arguments and calls `setup_logging(ui_mode=False)`
   (`main.py:354-377`).
2. `HeadlessService(...)` is constructed. Its constructor maps `port=0` to
   **4001**, creates `~/Downloads`, and stores config
   (`headless.py:111-188`). No host or queues exist yet.
3. `run_headless_in_thread()` starts a **daemon thread** running
   `trio.run(headless_service.start)` and polls up to 30 s for
   `ready`/`failed` (`main.py:56-88`).
4. `TornadoServer(...)` is constructed on the main thread. It assigns
   `service.on_stopped` and loads the optional RAG vector store
   (`tornado_server.py:138-147`).
5. `TornadoServer.start()` calls `self._app.listen(self.port)` with no bind
   address, i.e. all interfaces (logged as `0.0.0.0`) and blocks in the IOLoop
   (`tornado_server.py:156-166`).
6. In the trio thread, `HeadlessService.start()` creates bounded janus queues,
   opens `trio_asyncio`, sends a system message, and calls `_run_service()`
   (`headless.py:202-238`).
7. `_run_service()`:
   - derives an Ed25519 key from `sha256(seed)` (`headless.py:289-291`);
   - builds `listen_addr = /ip4/0.0.0.0/tcp/<port>` and
     `announce_addr = /ip4/<announce_host>/tcp/<port>` (`headless.py:294-295`);
   - warns if the port is already in use (`headless.py:240-265, 297`);
   - creates the host with `new_host(key_pair=..., connection_config=...)`
     (`headless.py:309-313`);
   - registers the identify stream handler (`headless.py:316-319`);
   - creates `KadDHT(..., DHTMode.SERVER, enable_random_walk=True)`
     (`headless.py:322`);
   - computes `full_multiaddr` **before** the listener starts
     (`headless.py:325`);
   - creates GossipSub, Pubsub, Bitswap client, MerkleDag
     (`headless.py:336-356`);
   - enters `host.run(listen_addrs=[listen_addr])` (`headless.py:359`);
   - starts pubsub/gossipsub/DHT background services, waits for pubsub readiness,
     starts `BootstrapDiscovery`, joins the chat room (subscribes to the chat and
     discovery topics), starts Bitswap, dials `--connect` addresses, then sets
     `ready = True` and `ready_event` (`headless.py:360-389`);
   - starts polling tasks for messages, outgoing messages, topic subscriptions,
     peer connections, file shares/downloads, peer monitoring, then waits for
     `stop_event` (`headless.py:392-410`).
8. `/api/v1/service/stop` sets `service.stop_event` from the Tornado thread
   (`api/service.py:43-64`).
9. `_wait_for_stop()` notices the event, the nursery is cancelled, background
   services and `host.run()` exit, and `start()` finally clears `ready`/`running`
   and calls `on_stopped` (`headless.py:993-998, 224-238`).
10. `on_stopped` stops the Tornado IOLoop (`tornado_server.py:149-154`), so
    `server.start()` returns and `main()` exits 0, or exits 1 if the headless
    service is marked failed (`main.py:402-406`).

**Verified lifecycle ownership:** the API does not create a second host. The
Tornado adapter calls methods on the same `HeadlessService` instance.

### 3.2 Textual and Kivy modes

Both follow the same construction/thread pattern, then block in the UI on the
main thread (`main.py:408-486`).

**Important gap (verified by code trace and runtime):** after `ui.run()` or
`run_kivy_ui()` returns, `main()` exits without calling
`headless_service.stop()`. The headless thread is a daemon, so the process exit
kills it abruptly. There is no graceful `host.close()`, queue close, or storage
flush for these modes. See §3.5 for the measured result:
`ready=True, running=True, stop_event=False` at process exit.

### 3.3 Simple CLI and headless modes

- `--headless` runs `await headless_service.start()` in the main trio thread and
  has **no SIGINT/SIGTERM handler**. Ctrl-C raises `KeyboardInterrupt`; SIGTERM
  terminates without application-level cleanup. **Verified:** sending SIGTERM
  produced exit code 143 and zero shutdown log lines.
- Simple CLI runs `run_simple_interactive()` in a nursery with a message monitor
  and a blocking `input()` thread (`main.py:142-258`). The input task calls
  `await headless_service.stop()` after quit, but the monitor task does not
  observe `stop_event`; the nursery therefore waits forever. **Verified:** a
  piped `quit` still left the process alive after 10 seconds; it had to be
  killed with SIGTERM.

### 3.4 Partial failure and double-start behaviour

- Startup exceptions are caught in `start()`, which marks `failed = True`,
  clears flags, and re-raises (`headless.py:224-238`). Context managers in
  `_run_service()` clean up services that were already entered, but there is no
  explicit `host.close()` path for a failure before `host.run()` is entered.
- There is no guard against calling `start()` twice on the same instance; queues
  and a second host would be created.
- There is no `atexit`/signal-based graceful shutdown.
- The advertised `full_multiaddr` is computed before the listener is ready; it
  is only exposed through `get_connection_info()` after `ready = True`, so the
  API does not publish it early, but the startup log does.

### 3.5 Empirically verified lifecycle runs (2026-11-08)

These runs were performed on the working tree before any application code was
changed. Each used a distinct `--seed` and unused ports.

| # | Scenario | Command shape | Result |
|---|---|---|---|
| 1 | API graceful stop | `main.py --api --port 48101 --api-port 48102` then `POST /api/v1/service/stop` | **HTTP 200; exit 0**; one host created; shutdown began ~100 ms after the request |
| 2 | Textual teardown | `main.py --ui` with a fake `ChatUI.run()` that returns immediately (`--port 48152`) | **exit 0**; at return and after `main()`: `ready=True, running=True, stop_event=False` |
| 3 | Kivy teardown | `main.py --kivy` with a fake `kivy_ui` module (`--port 48153`) | **exit 0**; at return and after `main()`: `ready=True, running=True, stop_event=False` |
| 4 | Headless SIGTERM | `main.py --headless --port 48111`, wait ready, `kill -TERM` | **exit 143**; zero shutdown log lines |
| 5 | Simple CLI quit | `printf 'quit\n' \| main.py --port 48121` | **still alive after 10 s**; killed externally (exit 143) |
| 6 | Invalid peer port | `main.py --api --port 99999 --api-port 48132` | **exit 1 in ~1 s**; `StringParseError: Invalid MultiAddr ... integer not in range` |
| 7 | Partial startup failure after host creation | `main.py --api --topic universal-connectivity-browser-peer-discovery --port 48161 --api-port 48162` | **exit 1 in ~2 s**; host was created, ChatRoom raised `ValueError`; both ports free after exit |
| 8 | API bind conflict after headless ready | block `0.0.0.0:48172`, then `main.py --api --port 48171 --api-port 48172` | **exit 1**; `OSError: [Errno 48] Address already in use`; no shutdown logs |

#### Run 1 — API shutdown is process-clean but service-incomplete

The stop endpoint returned success and the process exited 0. The only shutdown
line in the log was:

```text
headless - Stop signal received, shutting down...
```

There was **no** `Stopping headless service...`, `ChatRoom stopped`, or
`Headless service stopped` line. This is expected from the code:
`api/service.py:54-64` sets `stop_event` directly and never calls
`HeadlessService.stop()`. The host and listeners are closed only because
`_run_service()` exits the `async with self.host.run(...)` context
(`headless.py:359`). `ChatRoom.stop()`, `BitswapClient.stop()`, queue closure,
and subscriber cleanup in `HeadlessService.stop()` (`headless.py:1498-1539`)
are bypassed. Process exit hides this; an embedded runtime or a repeated
in-process start/stop would leak or reuse stale objects.

Also verified: exactly **one** `Host created with PeerID` log line per API
process, and `/api/v1/node/info` returned the hand-built loopback multiaddr
`/ip4/127.0.0.1/tcp/48101/p2p/12D3Koo...`.

#### Runs 2 and 3 — TUI and Kivy leave the service running

The real UI event loop was replaced with a stub that returns immediately, so the
lifecycle boundary could be observed without a TTY/window. In both modes:

```text
UI_RETURN  ready=True running=True stop_event=False
AFTER_MAIN ready=True running=True stop_event=False
```

`main()` returned with the service still fully running, and the process exited
0 because `run_headless_in_thread()` uses a **daemon** thread
(`main.py:66`). The OS tears down the listener; the application performs no
ordered shutdown. This is a real lifecycle bug, not only a theoretical one.

#### Run 4 — SIGTERM is not graceful

`--headless` has no signal handler, so SIGTERM produced exit 143 and no
`Stop signal received` or host-closure logging. Container/systemd shutdown in
this mode would be abrupt until a signal handler is added.

#### Run 5 — Simple CLI does not exit after `quit`

The input task ran `HeadlessService.stop()` (logs show `Stopping headless
service...`, `ChatRoom stopped`, `Headless service stopped`), but the
`monitor_message_queues` task kept running (`main.py:160-210`), so the nursery
never completed. The process was still alive after 10 seconds. This confirms
the earlier code-trace concern.

#### Runs 6–8 — Failure paths

- An invalid port fails fast (good) but surfaces a raw multiaddr
  `StringParseError` with a full traceback rather than a validated
  configuration error.
- A failure **after** the host started (reserved topic) marks the service
  failed, exits 1, and releases both ports. Context-manager cleanup is
  sufficient at process level.
- A failure in the **API adapter** after the headless service is already ready
  exits 1 but never signals or stops the service. The peer listener stayed open
  until process exit. This is the same ownership gap as Run 1.

#### Startup timing observation

The API server starts only after `ready_event` is set, which is correct. In the
measured run, readiness was set at `24,027`, `main` observed it at `24,102`
(75 ms, bounded by the 200 ms poll loop), and Tornado was listening at `24,234`
(~130 ms after the launch decision). The stop request was served at `24,431`
and the service logged `Stop signal received` at `24,531` (~100 ms).

However, readiness itself was delayed ~2.5 s while `BootstrapDiscovery.start()`
resolved the hardcoded bootstrap list; all five `dnsaddr` entries failed with:

```text
Failed to query TXT records for _dnsaddr.bootstrap.libp2p.io: trio not found or too old
```

One connected peer appeared in the peerstore and routing table. Since all five
`dnsaddr` entries failed resolution, this is consistent with the single IPv4
bootstrap entry working; bootstrap is therefore partially functional. Phase 1
should identify the connected peer explicitly and decide whether readiness
should block on bootstrap DNS at all.

### 3.6 Trace against the brief's 13 lifecycle steps

| Brief step | Current implementation | Verified status |
|---|---|---|
| 1. User launches entry point | `main.py` argparse branches | Yes |
| 2. Configuration loaded | argparse + `load_dotenv()` from CWD | Yes; no config file/validation layer |
| 3. Identity loaded or created | `sha256(seed)` → Ed25519 key pair, every launch | Yes; nothing is loaded from disk |
| 4. Storage opened | none | **Absent** |
| 5. libp2p host created | `new_host(...)`, one call | Yes; exactly one per process |
| 6. Transports registered / listeners started | TCP default; `host.run(listen_addrs=[...])` | Yes (TCP/IPv4 only) |
| 7. Protocol handlers registered | identify handler, pubsub, gossipsub, DHT, Bitswap | Yes |
| 8. API server starts | after `ready_event`; `app.listen(port)` | Yes; all interfaces |
| 9. UI starts | after `ready_event` in UI modes | Yes |
| 10. Shutdown received | API stop endpoint, Ctrl-C; no SIGTERM | Partial |
| 11. UI/API stop | API: `on_stopped` stops IOLoop. TUI/Kivy: no stop call. | **Incomplete** |
| 12. Host stops / listeners close | via `host.run` context on API; via process exit on UI/headless | Partial |
| 13. Storage flushes / closes | no storage; queues/ChatRoom/Bitswap not closed on API path | **Incomplete** |

### 3.7 Ownership conclusion

There is **no duplicated host creation**: one `HeadlessService` owns one host,
and no adapter creates a second one. The problem is the opposite — lifecycle
ownership is fragmented:

- `HeadlessService` owns the host and the stop flag.
- `main.py` owns the thread and never joins it.
- `TornadoServer` owns the IOLoop and is coupled to the service through
  `on_stopped`.
- Textual/Kivy own the main thread and return without stopping the service.
- Simple CLI calls `stop()` from inside the runtime, before the service has
  actually wound down.

This is exactly the condition Phase 2's `ApplicationRuntime.start()/stop()`
must replace with a single owner.

### 3.8 Lifecycle remediation (implemented after this audit)

The gaps in §3.2–§3.7 were fixed without introducing a full runtime layer. The
ownership rule is now: **anything may *request* stop, but only the trio thread
that owns the host performs teardown, exactly once.**

| Change | File | Effect |
|---|---|---|
| `HeadlessService.request_stop()` | `headless.py` | Thread-safe, idempotent stop signal (sets `stop_event`). Used by UI teardown, signal handlers, and the API. |
| `HeadlessService._shutdown_async()` | `headless.py` | Single teardown path: ChatRoom, Bitswap, subscriber queues, legacy queues. Guarded by `_shutdown_done`. |
| `start()` finally block | `headless.py` | Runs `_shutdown_async()` under a shielded cancel scope on every exit path; then clears `ready`/`running` and fires `on_stopped`. |
| Double-start guard | `headless.py` | Second `start()` on the same instance raises `RuntimeError`. |
| `stop()` semantics | `headless.py` | Now signals only; kept async for existing trio callers and never blocks on its own teardown. |
| `stop_headless_and_join()` | `main.py` | UI/API modes request stop and join the trio thread (10 s bound) instead of letting a daemon thread die with the process. |
| SIGINT/SIGTERM handlers | `main.py` | Headless and API modes map both signals to a graceful stop; API also wakes the Tornado IOLoop via `TornadoServer.stop()`. |
| Simple CLI monitor loop | `main.py` | Exits when `stop_event` is set, so typing `quit` completes the nursery. |
| API stop endpoint | `api/service.py` | `/service/stop` calls `request_stop()` instead of mutating service state directly. |
| `TornadoServer.stop()` | `tornado_server.py` | Thread-safe IOLoop stop used by the API signal handler. |

Re-verified behaviour after the change (same method as §3.5):

| Scenario | Before | After |
|---|---|---|
| Textual teardown | process exit with service still running | `ready=False, running=False, stop_event=True, shutdown_done=True`; logs `ChatRoom stopped`, `Headless service stopped` |
| Kivy teardown | same as Textual | same as Textual |
| Headless SIGTERM | exit 143, no shutdown logs | exit 0; `Received signal 15` → full teardown |
| Simple CLI `quit` | alive after 10 s | exits 0; full teardown |
| API `/service/stop` | only `Stop signal received` | full teardown logs; exit 0 |
| API SIGTERM | abrupt exit | exit 0; full teardown |
| API SIGINT | (invalid shell test) | exit 0; `Received signal 2` → full teardown |
| API bind conflict | peer left running until process exit | teardown logs, exit 1, peer port free |

Measured teardown time after `request_stop()`: **~0.07 s** for a ready peer with
no bootstrap connections (ChatRoom + Bitswap + all queues). Startup latency is
unchanged and still dominated by bootstrap DNS (see §3.5).

Lifecycle tests added in `tests/test_headless_lifecycle.py`:

- teardown runs when `_run_service` raises;
- `request_stop()` is idempotent and host-free;
- a second `start()` raises;
- the installed SIGTERM handler requests stop;
- starting a real host on a free port and calling `request_stop()` from another
  thread joins the trio thread, clears `ready`/`running`, and closes queues.

Remaining lifecycle limitations (out of scope for this fix):

- Textual/Kivy still own their own event loops; SIGTERM during an active UI is
  handled by the toolkit, not by these handlers.
- Simple CLI still blocks in the `input()` thread; SIGTERM while parked there
  is not converted into a graceful stop.
- `stop()` is one-shot: a stopped instance cannot be restarted (the guard is
  intentional; use a new instance).
- There is still no `ApplicationRuntime` abstraction; Phase 2 remains the place
  to consolidate mode-specific glue.

---

## 4. UI and communication-path inventory

| Interface | Framework | Process model | Talks to backend via | Creates host? | Shutdown |
|---|---|---|---|---|---|
| Textual TUI (`ui.py`) | Textual | Same process, main thread | Direct method calls + `message_queue`/`system_queue` (janus sync side) | No | Textual exit; headless thread not explicitly stopped |
| Kivy UI (`kivy_ui.py`) | Kivy + KivyMD | Same process, main thread | Direct method calls + janus queues + service query methods | No | Kivy exit; headless thread not explicitly stopped |
| Simple CLI | stdin/stdout | Same process, trio | Direct method calls + janus queues | No | Input task calls `stop()`, but monitor-task exit is questionable |
| Headless | none | Same process, trio | none | No (service owns host) | No signal handler |
| HTTP API (`tornado_server.py` + `api/`) | Tornado | Same process, main thread; service in trio thread | Direct Python calls into `HeadlessService` | No | `on_stopped` stops IOLoop |
| React frontend (`py-peer-frontend/`) | React 18 + Vite + TypeScript | Browser, separate process | `fetch` to `/api/v1/*`, `WebSocket` to `/ws/*` | No | Browser page lifecycle only |

### 4.1 API surface (verified from `tornado_server.py:52-114`)

- **Node:** `/api/v1/node/{info,status,multiaddr}`
- **Peers:** list/count/known/connect/detail/identify
- **Messages:** send default, per-topic GET/POST, unread, mark read
- **Topics:** list/subscribe, info, mesh peers
- **Files:** shared list/detail, share, download, multipart upload
- **DHT:** status, peers, routing table
- **PubSub:** peers, mesh, fanout, config, subscriptions
- **Identity:** cache list/detail/public key/delete
- **Service:** status, config, stop, bootstrap
- **RAG:** `POST /api/v1/ask` (optional)
- **WebSockets:** `/ws/messages`, `/ws/system`, `/ws/peers`, `/ws/pubsub/mesh`

### 4.2 React frontend behaviour

- Base URL comes from `VITE_API_URL`; in dev Vite proxies `/api` and `/ws` to
  `localhost:8765` (`vite.config.ts:6-19`, `src/api/client.ts:70-76`).
- Initial node/status load happens once; failure shows a Retry button that
  reloads the page (`PyPeerContext.tsx:64-86`, `Booting.tsx:8-17`).
- Topics and peers are polled every 5 s; message and peer WebSockets reconnect
  after 3 s on close (`PyPeerContext.tsx:88-110, 135-175`).
- The client performs no business logic beyond presentation, optimistic message
  insertion, unread bookkeeping, and `markRead` calls.

### 4.3 Adapter duplication

There is **no duplicated networking logic** in the UIs: all packet/host work is
in `HeadlessService`/`ChatRoom`. However, there is also **no service layer**:
UI code calls the concrete `HeadlessService` directly, and Tornado handlers call
it directly. There is no interface/contract separating application operations
from the runtime implementation.

---

## 5. Host creation and protocol inventory

### 5.1 Host creation sites

**Verified:** exactly one host construction call exists in application code:

- `headless.py:309` — `self.host = new_host(key_pair=key_pair, connection_config=config)`

No other module imports or calls `new_host`. The API, Textual, and Kivy adapters
never create a host.

### 5.2 Protocol inventory

| Protocol / feature | Present? | Configured? | Notes |
|---|---|---|---|
| TCP transport | Yes | Implicit default | `new_host()` default is TCP-only; listener `/ip4/0.0.0.0/tcp/<port>` |
| QUIC | Available in libp2p | No | `enable_quic` not passed |
| WebSocket transport | Available in libp2p | No | `enable_websocket` not passed |
| WebRTC / WebTransport | Available in libp2p | No | flags not passed |
| IPv6 listener | No | No | only `/ip4/...` listen address |
| Identify | Yes | Yes | handler registered; outbound request helper exists |
| KadDHT | Yes | Yes | `DHTMode.SERVER`, random walk enabled |
| DHT application records | No | No | no `dht.put_value`/provider calls found |
| GossipSub | Yes | Yes | protocols v1.0, v1.1, v1.2; tuned mesh parameters |
| Pubsub floodsub/other routers | No | No | only GossipSub is constructed |
| Bootstrap discovery | Yes | Yes | hardcoded `BOOTSTRAP_PEERS` list |
| mDNS / UPnP / AutoNAT / AutoTLS | No | No | not enabled |
| Circuit relay / DCUtR hole punching | No app configuration | No | not configured |
| Bitswap + MerkleDAG | Yes | Yes | `MemoryBlockStore` (in-memory blocks) |
| Ping | No | No | no handler or keepalive wiring found |
| Custom application protocol | No | No | chat uses GossipSub |

### 5.3 Transport matrix (preliminary Phase 1 input)

| Transport | Implemented in installed libp2p | Registered by this app | Listener starts | Inbound verified | Outbound verified | Advertised correctly |
|---|---|---|---|---|---|---|
| TCP/IPv4 | Yes | Yes (implicit default) | Yes, in code | **Not tested** | **Not tested in Phase 0** | **No by default** (loopback announcement) |
| QUIC | Yes | No | No | N/A | N/A | N/A |
| WebSocket | Yes | No | No | N/A | N/A | N/A |
| WebRTC | Yes | No | No | N/A | N/A | N/A |
| WebTransport | Yes | No | No | N/A | N/A | N/A |
| IPv6/TCP | Yes (library) | No | No | N/A | N/A | N/A |

"Not tested" means no automated or recorded manual test exists in this audit.
This matrix is **preliminary**; Phase 1 must verify listener startup, inbound,
and outbound behaviour directly.

---

## 6. Address discovery and advertisement

### 6.1 Verified address flow

- **Bind address:** `/ip4/0.0.0.0/tcp/<port>` (`headless.py:294`).
- **Port default:** `HeadlessService.__init__` converts `0` to `4001`
  (`headless.py:113`). `DEFAULT_PORT = 9095` exists but is unused
  (`headless.py:76`). README examples still show `9095`, while its own note says
  peers default to `4001`.
- **Advertised address:** constructed manually as
  `/ip4/<announce_host>/tcp/<port>/p2p/<peer_id>` (`headless.py:295, 325`).
  `announce_host` defaults to `"127.0.0.1"` in both `main.py` and
  `HeadlessService` (`main.py:303-310`, `headless.py:111`).
- **Observed addresses:** identify responses are cached for API display
  (`headless.py:1414-1462`), but observed addresses are **not** used to update
  the advertised address.
- **AddrsFactory / announce_addrs:** not used, even though the installed
  py-libp2p 0.8.1 supports both (`new_host` signature).
- **host.get_addrs():** not used to produce the application's advertised
  multiaddr.
- **Wildcard normalization:** dial strings containing `/ip4/0.0.0.0/` or
  `/ip6/::/` are rewritten to loopback before dialing (`headless.py:267-285`).

### 6.2 Consequences

- A peer started with defaults advertises loopback. A peer on another machine
  cannot dial that announcement.
- LAN use requires `--announce-host <LAN IP>` manually.
- There is no public-IP discovery, NAT traversal, relay, or observed-address
  based announcement.
- There is no Docker/container handling; a container IP could be advertised if
  `--announce-host` is set to it, but nothing detects or corrects that.
- IPv6 is not supported by the listener or announcement.
- Listener readiness and advertisement correctness are not observed/tested; the
  string is computed before `host.run()`.

### 6.3 Bootstrap list

`BOOTSTRAP_PEERS` is hardcoded in `headless.py:85-92` and mixes `dnsaddr`
addresses with one IPv4 TCP address. `filter_compatible_peer_info()` exists but
is never called (`headless.py:94-103`). Whether all listed addresses are
dialable with the installed fork is **unknown**; Phase 1 must test it.

---

## 7. Persistence and data inventory

**Verified:** there is no database, ORM, migration system, or identity file in
the application. The only file writes are the system log, downloaded/uploaded
files, and an optional frontend build.

| Data | Owner | Current storage | Survives restart? | Can be reconstructed? | Secrets? | Notes |
|---|---|---|---|---|---|---|
| Private peer identity | `_run_service` | Derived from `sha256(seed)` each launch | Only if same `--seed` used | Yes from seed | **Seed is logged at INFO** (`headless.py:188`) | Default seed `"py-peer"` is shared by all default peers → peer-ID collision |
| Peer ID | libp2p host | Derived from key | Only with same seed | Yes | No | No persisted key file |
| Chat messages | `HeadlessService.topic_messages` | In-memory list | **No** | No | Maybe (message content) | Capped at 1000/topic (`MAX_TOPIC_MESSAGES`) |
| Unread counts | `topic_unread_counts` | In-memory dict | **No** | No | No | Reset on restart |
| Read flags | message dicts | In-memory | **No** | No | No | |
| Shared file metadata | `shared_files` | In-memory dict | **No** | No | Local paths held server-side | API hides `filepath` |
| File blocks | Bitswap `MemoryBlockStore` | In-memory | **No** | No (unless re-shared from disk) | Maybe | Large files consume RAM |
| Shared/downloaded files | `~/Downloads` | Filesystem | Yes | N/A | Maybe | Not under app data dir |
| Identify cache | `peer_info_cache` | In-memory | **No** | Yes by reconnecting | Public keys (not private) | 5-minute freshness check |
| DHT routing table | KadDHT | In-memory | **No** | Yes from network | No | Rebuilt via random walk/bootstrap |
| DHT application records | — | None | N/A | N/A | N/A | App does not store DHT records |
| Pubsub mesh / connections | libp2p | In-memory | N/A | Yes | No | Ephemeral by design |
| Topic subscriptions | `ChatRoom.subscriptions` | In-memory | **No** | No | No | Only CLI `--topic` survives via config |
| System log | `chatroom.system_logger` | `system_messages.txt` in **CWD** | Yes | N/A | Possibly | Rotating: 1 MB × 3, created at import |
| Application settings | CLI args + `.env` | CWD `.env` for Groq key | Yes if `.env` kept | N/A | `GROQ_API_KEY` | No `UC_*` config, no config file |
| RAG vector store | `rag_handler` | Package-relative `llm/codes/libp2p_vectorstore` | Read-only | Rebuildable | No | Path is inside source tree |
| API credentials | Groq SDK | Environment | Depends on env | N/A | **Yes** | Never log it |

### 7.1 Writable-path policy (current)

- `system_messages.txt` → current working directory, at `chatroom` import time
  (`chatroom.py:24-35`). Running from a different directory creates a new log
  there.
- `.env` → current working directory (`main.py:17-18`).
- Downloads/uploads → `~/Downloads` (`headless.py:58,162-163`;
  `api/files.py:178-188`).
- Package source directory is used for the RAG vector store path
  (`rag_handler.py:21-23`), but it is read-only at runtime.
- No `UC_DATA_DIR`, no platformdirs call, no permission hardening, no
  writability preflight.

### 7.2 Delivery semantics

GossipSub is used as a live pub/sub transport only. A message is stored in
memory when it is observed on a subscribed topic; there is no acknowledgement,
durable queue, offline delivery, or synchronization mechanism. Any claim of
persistent chat history or offline delivery would be false today.

---

## 8. Kivy and dependency audit

### 8.1 Kivy usage

- Kivy is imported only by `kivy_ui.py` (top-level imports of `kivy.*` and
  `kivymd.*`). `main.py` imports `kivy_ui` lazily only under `--kivy`
  (`main.py:437-447`).
- Core modules (`headless.py`, `chatroom.py`, `api/*`, `tornado_server.py`,
  `ui.py`, `main.py`) do **not** import Kivy.
- Kivy UI talks to the same in-process service through direct calls and janus
  queues, like the Textual UI.
- No Buildozer, `.spec`, python-for-android, or Android/iOS packaging files are
  present.
- README claims mobile/Kivy support and shows screenshots, but no Android build
  is verified or configured in this repository.

### 8.2 Dependency impact

Direct runtime dependencies (`pyproject.toml:7-20`):

```text
textual, libp2p (fork via uv.sources), trio, base58, protobuf, janus,
trio_asyncio, kivy, kivymd, python-dotenv, groq, tornado
```

Verified installed versions in `.venv`:

```text
libp2p 0.8.1 (fork branch fix/peerstore-local-identity)
textual 3.5.0
trio 0.30.0
trio_asyncio 0.16.0
janus 2.0.0
tornado 6.5.10
protobuf 6.33.6
kivy 2.3.1
kivymd 2.0.0
```

KivyMD pulls `asynckivy`, `materialshapes`, `materialyoucolor`, and Pillow
(`uv.lock`). Removing `kivy` and `kivymd` would remove that chain; no other
module uses those packages.

### 8.3 Packaging findings

- `uv build` succeeds, but `packages = ["."]` makes the wheel include the whole
  directory.
- Verified wheel contents: **13,646 entries**, **36 MB compressed**, **129.1 MB
  uncompressed**, including **13,572 `node_modules` files**, the built frontend
  `dist/`, tests, `API_REFERENCE.md`, and `system_messages.txt`.
- The wheel metadata lists `libp2p<0.9,>=0.8.1` with no git URL, while
  `uv.lock` resolves the fork branch. A `pip install` from the wheel would not
  reproduce the tested libp2p source. This is a reproducibility risk for Phase 8.
- `system_messages.txt` is included in the wheel because it is untracked but not
  gitignored. Runtime logs must not be package data.

---

## 9. Configuration, logging, and security

### 9.1 Configuration

- Source of configuration: CLI flags in `main.py`; `.env` loaded from CWD;
  hardcoded constants in `headless.py`/`chatroom.py`.
- No config file, no precedence rules, no validation layer, no documented
  defaults beyond `--help` and README.
- Networking is not configurable beyond `--port`, `--announce-host`, `--seed`,
  `--topic`, `--connect`, `--no-strict-signing`.
- API bind address is not configurable; `TornadoServer.start()` binds all
  interfaces. API port is configurable.

### 9.2 Logging

- `main.setup_logging()` uses `logging.basicConfig`; console handler disabled in
  UI modes; null handler otherwise (`main.py:26-43`).
- Hardcoded DEBUG levels for many `libp2p.*` loggers at import
  (`main.py:46-54`).
- `system_messages.txt` is the only file log; it is CWD-relative, rotating, and
  written at `chatroom` import.
- No structured logging, request IDs, log levels per module, or log directory.
- **Sensitive data risk:** the deterministic identity seed is logged at INFO
  (`headless.py:188`). Anyone with the log can recreate the private key.

### 9.3 Security posture (verified)

- HTTP API has **no authentication or authorization**.
- CORS is `*`, with `X-API-Key` allowed but never checked (`api/base.py:54-62`).
- WebSocket `check_origin()` always returns `True` (`api/websocket.py:33-34`).
- Tornado binds all interfaces, not loopback.
- Request body limit is 32 MB; uploads are capped at 25 MB
  (`tornado_server.py:116-123`, `api/files.py:18`).
- Local file sharing is restricted to the download directory
  (`api/files.py:74-88`); upload/download filenames are sanitized and checked
  for path containment (`api/files.py:164-188`, `headless.py:928-955`).
- API error responses avoid exposing 5xx tracebacks (`api/base.py:118-137`).
- Private key bytes are not exposed by the API, but the seed is logged.

**Conclusion:** the API is a trusted-local-development interface today. Exposing
it beyond loopback requires authentication, bind-address control, origin policy,
and likely rate limiting before Phase 7/Docker or remote MCP use.

---

## 10. Baseline tests and checks

### 10.1 Environment

```text
macOS 27.2 (arm64)
Python 3.12.2
uv 0.13.5
ruff 0.15.13 (system; not in .venv)
Node v26.12.1 / npm 11.20.0
```

### 10.2 Commands and results (run before any code change)

| Check | Command | Result |
|---|---|---|
| Python unit tests | `.venv/bin/python -m unittest discover -s tests -v` | **PASS** — 25 tests, OK (0.118 s) |
| Python lint | `ruff check . --exclude .venv --exclude py-peer-frontend --exclude __pycache__` | **FAIL** — 83 errors: 65 F541, 12 F401, 2 E402, 2 E722, 2 F841 (77 auto-fixable) |
| Frontend lint | `npm run lint` (in `py-peer-frontend`) | **PASS with warning** — 0 errors, 1 warning `react-refresh/only-export-components` at `PyPeerContext.tsx:37` |
| Frontend build/typecheck | `npm run build` (runs `tsc && vite build`) | **PASS** — 907 modules, built in ~0.7 s; stale Browserslist data warning |
| Package build | `uv build` | **PASS technically** — sdist + wheel built; wheel is 36 MB / 13,646 entries and wrongly contains `node_modules` |
| Python type check | none configured | N/A |

The lifecycle test intentionally logs a `StreamFailure` traceback; the test
passes and asserts the exception propagates and the service is marked failed.

### 10.3 Test coverage gaps

Verified absent:

- No test starts a real `HeadlessService` host/listener.
- No test dials a second peer or verifies inbound GossipSub.
- No test covers the Tornado API handlers end-to-end.
- No test covers the Textual/Kivy adapters or React UI.
- No test covers identity persistence (there is none).
- No test covers data-directory behaviour (there is none).
- No test covers shutdown, signal handling, or repeated start/stop.
- No test covers packaging contents or installed console-script behaviour.
- No py-peer CI workflow runs any of these checks.

The existing tests are useful regression tests for bounded history, queue
behaviour, path containment, topic filtering, startup failure, and JSON
validation. They are currently **untracked** (`?? py-peer/tests/`).

---

## 11. Risks and constraints for the refactor

### 11.1 Identity migration risk (highest)

The current peer ID is a pure function of `--seed`, with default `"py-peer"`.
Moving to a persisted random key would change every user's peer ID and break
existing dial addresses. The implementation brief explicitly says not to
silently regenerate identity. Before Phase 4, choose and document one of:

1. **One-time migration:** if a seed-derived identity exists, persist that key
   to the new key store so the peer ID is preserved.
2. **Explicit opt-in:** keep seeded identities for development and add a
   separate persistent identity for production, with clear docs.
3. **Break compatibility intentionally:** document the peer-ID change and
   require users to re-share multiaddrs.

Do not choose this silently. Also stop logging the seed.

### 11.2 Address and deployment risk

Changing advertised addresses affects dialability. The current default is
loopback, so the existing "working" local demos do not prove LAN/remote
behaviour. Phase 1 must verify listener/inbound/outbound and define the
supported network model before Docker work.

### 11.3 Storage migration risk

There is no existing database to migrate, but message history is currently lost
on restart. Adding SQLite must not imply network-wide offline delivery. Phase 3
must define message identity, dedupe, retention, and delivery semantics.

### 11.4 Lifecycle risk

Resolved by the §3.8 remediation:

- TUI/Kivy exits now request stop and join the trio thread (verified). The
  service reports `ready=False, running=False` after `main()` returns.
- Headless and API modes handle SIGINT/SIGTERM gracefully (verified).
- Simple CLI exits cleanly after `quit` (verified).
- API `/service/stop` now goes through `request_stop()` and the single teardown
  path, so ChatRoom, Bitswap, and queues are closed (verified).
- API adapter startup failure after the peer is ready now stops the peer
  (verified: bind conflict exits 1 with a free peer port).
- A double-start guard exists.

Still open:

- Textual/Kivy SIGTERM during an active UI and SIGTERM while the simple CLI is
  parked in `input()` are not graceful.
- No automated test covers repeated instance start/stop or signal delivery.
- No `ApplicationRuntime` owner yet; Phase 2 should consolidate the remaining
  per-mode glue.

### 11.5 Packaging/reproducibility risk

- Wheel includes `node_modules`, `dist`, tests, and logs.
- `uv.sources` fork is not represented in wheel metadata.
- No clean-install/console-script test.

### 11.6 API/security risk

- Unauthenticated API bound to all interfaces with wildcard CORS and WebSocket
  origins. Safe only on a trusted local machine. Must be resolved before Docker
  publishes the API port or before adding MCP.

### 11.7 Concurrency/consistency risk

- The service runs on trio while Tornado runs on asyncio in another thread.
  Shared state is partly protected by `RLock`, but some adapters read live
  objects directly (for example `api/peers.py:117` reads `peer_info_cache`
  without the lock; peerstore access happens from the Tornado thread).
- Legacy janus queues are single-consumer. They work because only one local UI
  consumes them, but a future in-process API + TUI combination would compete.
- WebSocket fan-out drops events for slow clients by design (bounded queues).

### 11.8 Documentation drift

- README examples use port `9095`; actual default is `4001`
  (`headless.py:113`; `DEFAULT_PORT = 9095` is unused).
- README says "Message persistence across sessions" is a roadmap item; that is
  consistent with the code.
- README describes Kivy as mobile-ready, but no Android packaging or CI exists.

---

## 12. Recommended migration sequence (file-level)

This follows the brief's phase order. Do not delete Kivy or write Docker first.

### Phase 1 — Transport/address correctness (next)

**Goal:** prove TCP listener and inbound/outbound behaviour, then fix
announcement.

Likely touch points:

- `headless.py`: listener/announce construction, `new_host` options, observed
  address handling, startup logging, readiness ordering.
- `main.py`: announce/listen configuration flags and validation.
- New tests: start one/two hosts on distinct ports, assert listener bound,
  connect, publish/receive, assert advertised address contents.

Must decide: default `announce_host` (loopback vs auto-detect LAN vs explicit
required), IPv4/IPv6 scope, and whether `host.get_addrs()`/`announce_addrs`/
`addrs_factory` should replace the hand-built string.

### Phase 2 — Unified service layer and shared runtime

Likely touch points:

- New runtime/service modules (for example `runtime.py`, `services/`), or a
  clearly named package.
- `headless.py`: split host/runtime ownership from API-facing query methods.
- `api/*.py`: call services, not `HeadlessService` internals.
- `ui.py`, `kivy_ui.py`, `main.py`: inject services instead of the concrete glue
  object.
- Tests: service-level tests with fake/in-memory dependencies; one operation
  exercised directly and through the HTTP adapter.

### Phase 3 — Storage design

Define message/peer/settings repositories, message identity/dedupe, retention,
and migration policy. Do not add a server database. Prefer SQLite with
non-blocking access, versioned migrations, and backup/restore docs.

### Phase 4 — Persistence and data directories

Likely touch points:

- Identity: use py-libp2p's `FileSystemKeyStore`/`auto_persist` (present in
  0.8.1) or an explicit key store; migrate seeded identity deliberately.
- Add `UC_DATA_DIR` and platformdirs-based defaults; move
  `system_messages.txt`, database, identity, and file storage out of CWD.
- Add writability/permission preflight and clear failure messages.
- Tests: restart preserves peer ID/history; unwritable dir fails safely;
  migration/backup/restore.

### Phase 5 — Remove Kivy from core

- Remove `kivy`/`kivymd` from mandatory dependencies; move `kivy_ui.py` to an
  optional extra or retire it.
- Verify `main.py`, `headless.py`, API, and Textual import/run in a clean venv
  without Kivy.
- Update README, screenshots, and support policy.
- Treat Android as a separate decision; do not claim removal alone fixes it.

### Phase 6 — UI adapters

- Textual/Kivy use service interfaces.
- React keeps HTTP/WS contracts; add reconnect/error tests where practical.
- Add headless mode without UI dependencies.
- Define API authentication/origin policy before remote exposure.

### Phase 7 — Docker/Compose

- Add Dockerfile, Compose, `.dockerignore`, health check, persistent `/data`
  volume.
- Publish only required ports; document TCP vs UDP needs and advertised
  addresses under bridge/host networking.
- Test recreation preserves identity and data.

### Phase 8 — Validation/release

- Clean install from wheel/sdist, console script outside repo.
- Full transport/address matrix, persistence, Docker, shutdown tests.
- Fix packaging (`packages`, exclude frontend/tests/logs) and pin the tested
  libp2p source reproducibly.

---

## 13. Unknowns and assumptions

### Unknowns (must be verified, not guessed)

1. Whether the hardcoded bootstrap peers (especially `dnsaddr` entries) are
   dialable with the installed fork.
2. Actual `host.get_addrs()` output for the wildcard listener and whether
   identify observed addresses would be usable behind NAT.
3. Whether two default peers (same seed) can coexist/connect without identity
   confusion in practice (peer-ID collision is expected from the code).
4. Whether a *new* instance can start cleanly after a previous instance stopped;
   a second `start()` on the same instance is now rejected by design.
5. Whether Bitswap file transfer works reliably at scale and whether in-memory
   blocks survive long transfers.
6. Actual behaviour of the fork's `FileSystemKeyStore`/`auto_persist` APIs and
   their compatibility with this code.
7. Whether `duplicate` messages are deduplicated by GossipSub or by the app
   (the app does not dedupe beyond its own echo handling).
8. Android feasibility and the actual Android build failure (no build files or
   logs exist here).

### Assumptions used only for scoping

- The product is primarily the Python peer; the other language peers are out of
  scope for this audit.
- "Chat history survival" is a desired product feature per the brief, not a
  current feature.
- Docker deployment is expected to run the headless/API runtime, not the React
  frontend bundle.
- The React frontend will continue to talk HTTP/WS rather than importing Python
  internals.

---

## 14. Phase 0 acceptance criteria

- [x] Actual package and entry points identified.
- [x] All UIs and their communication paths listed.
- [x] All host creation sites identified (one: `headless.py:309`).
- [x] Startup and shutdown paths documented for API/TUI/Kivy/CLI/headless.
- [x] Transport and advertised-address logic traced (TCP-only; manual
      announcement; default loopback).
- [x] Persistence status documented for each data category.
- [x] Kivy usage and dependency relationships documented.
- [x] Baseline test results recorded (25 pass, ruff 83 errors, frontend
      lint/build, package build).
- [x] Unknowns explicitly listed rather than guessed.

**Phase 0 conclusion:** the repository is understood well enough to begin
Phase 1, with two blocking decisions recorded above: identity migration policy
and the supported network/deployment model. No application code was changed in
this phase; the only addition is this audit document.
