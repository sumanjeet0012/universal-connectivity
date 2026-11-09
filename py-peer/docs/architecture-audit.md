# Universal Connectivity py-peer — Architecture Audit (Phases 0–7)

**Status:** Phase 0–7 deliverables
**Audit date:** 2026-11-08
**Repository:** `universal-connectivity` (`py-peer` is the subject of this audit)
**Baseline revision:** `dcfbf07` plus the current uncommitted working tree; Phase 1
changes are recorded in §15
**Auditor method:** source inspection, dependency inspection, package build, test/lint/build execution

> This document records the **current** system, not the target architecture in the
> implementation brief. Every claim below is marked as **Verified**, **Assumption**,
> or **Unknown** unless it is directly quoted from source or command output.
>
> Sections 1–14 preserve the Phase 0 audit of the pre-migration tree. Where a
> finding has since been fixed, the section carries a phase update. In
> particular, **Kivy was removed entirely in Phase 5** (§19); references to a
> Kivy adapter in the historical sections describe the baseline, not the
> current tree.
>
> The working tree is dirty. Several files (`main.py`, `headless.py`, `api/*`,
> `pyproject.toml`, the React frontend) already contain uncommitted changes, and
> `tests/`, `system_messages.txt`, and `.venv/` are untracked. Baseline results
> therefore describe the working tree, not commit `dcfbf07` alone.

---

## 1. Executive summary

The Python peer is a single-process application with a usable
`HeadlessService`, a Tornado REST/WebSocket API, a Textual TUI, a simple CLI,
and a separate React frontend. It works, but it does not yet meet the
implementation brief's architecture goals.

The most important verified findings are:

1. **One host is created per running `HeadlessService`**, in
   `headless.py:_run_service` (`headless.py:309`). No UI or API module creates a
   libp2p host. API mode and TUI mode are mutually exclusive CLI branches that
   each construct one `HeadlessService`.
2. **Only TCP/IPv4 is configured.** `new_host()` is called without QUIC,
   WebSocket, WebRTC, WebTransport, mDNS, UPnP, AutoTLS, or relay options. The
   installed libp2p 0.8.1 supports some of these behind flags, but this app does
   not enable them. *(Phase 1 update: still true. §15 records why each transport
   remains disabled and what is missing to enable it.)*
3. **The advertised address is built by hand and defaults to loopback**
   (`/ip4/127.0.0.1/tcp/<port>/p2p/<peer-id>`). It is not derived from the
   listener or from identify-observed addresses. Remote peers cannot dial the
   default announcement. *(Phase 1 update: **fixed**. Advertised addresses now
   come from `host.get_addrs()` with an `AddrsFactory` that rewrites wildcard
   listeners; see §15. The default is still loopback for backward compatibility,
   and the peer now logs a loud warning when it advertises loopback.)*
4. **No durable application storage exists.** Messages, unread counts, peer
   identify metadata, shared-file metadata, and Bitswap blocks are in memory.
   Identity is re-derived from a seed on every launch, and the default seed
   (`"py-peer"`) is shared by every peer that does not override it.
5. **Writable state depended on the current working directory.** Resolved by
   Phase 4 (§18): identity, history, and the system log live under one resolved
   data root (`--data-dir` / `UC_DATA_DIR` / platform default). `.env` is still
   read from CWD and downloads still go to `~/Downloads` by design.
6. **Kivy was a mandatory dependency** even though it was only imported by
   `kivy_ui.py` (deferred import in `main.py`). Resolved by Phase 5 (§19): the
   Kivy UI, `--kivy` flag, optional extra, and dependency chain were removed
   entirely. No Android/buildozer/python-for-android files exist, and no
   py-peer CI workflow exists.
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
| Kivy UI *(removed in Phase 5)* | `--kivy` *(removed)* | Formerly `HeadlessService` + Kivy; no longer present |
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
| `kivy_ui.py` *(removed in Phase 5)* | 1374 | Formerly the Kivy/KivyMD UI; deleted, see §19 |
| `rag_handler.py` | 178 | Optional ChromaDB + Groq `/api/v1/ask` endpoint |
| `py-peer-frontend/` | — | React + Vite + TypeScript client |
| `tests/` | 9 files, 82 tests | `unittest` unit/regression tests (committed in `841b20a`; `test_address_config.py` added in Phase 1) |

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

Verified absent in the py-peer working tree at the Phase 0 baseline (Phase 7
later added the container files; see §21):

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

- `headless.py:_run_service` (~line 555 after Phase 1) —
  `self.host = new_host(key_pair=..., connection_config=..., listen_addrs=...,
  announce_addrs=..., addrs_factory=...)`

No other module imports or calls `new_host`. The API and Textual adapters
never create a host.

### 5.2 Protocol inventory

| Protocol / feature | Present? | Configured? | Notes |
|---|---|---|---|
| TCP transport | Yes | Yes, default | `new_host(listen_addrs=...)` auto-detects TCP; default listener `/ip4/0.0.0.0/tcp/<port>`; `--listen-addr` overrides |
| QUIC | Available and experimentally verified (§15.4) | No — QUIC listen addresses rejected | Deliberate Phase 1 scope decision; enabling changes the transport mix |
| WebSocket transport | Available in libp2p, `websockets` missing | No | Listen addresses rejected; dependency not declared |
| WebRTC / WebTransport | Available in libp2p, `aiortc` missing | No | Listen addresses rejected; flags not passed |
| IPv6 listener | Yes via `--listen-addr` | Yes, when requested | `::1` verified at runtime (§15.4) |
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

### 5.3 Transport matrix (final — verified in Phase 1)

Evidence and test commands are in §15. "Available" means the module ships in the
installed py-libp2p fork; "enabled" means this app passes the flag.

| Transport | Available in installed libp2p | Enabled by this app | Listener starts | Inbound verified | Outbound verified | Advertised correctly |
|---|---|---|---|---|---|---|
| TCP/IPv4 (wildcard) | Yes | Yes (default) | Yes | Yes (two-peer test) | Yes (two-peer test) | Yes — rewritten to `announce_host` / `announce_addrs` |
| TCP/IPv4 (concrete bind) | Yes | Yes via `--listen-addr` | Yes | Yes (loopback test) | Yes (two-peer test) | Yes — advertised as bound |
| TCP/IPv6 loopback | Yes | Yes via `--listen-addr /ip6/::1/tcp/...` | Yes, verified | Not tested | Not tested | Yes — `/ip6/::1/tcp/<p>/p2p/<id>` |
| TCP/IPv6 wildcard | Yes | Possible via `--listen-addr /ip6/::/tcp/...` | Not tested | Not tested | Not tested | Factory supports it (unit-tested); runtime not exercised |
| QUIC | Yes (`aioquic 1.7.0` installed); experimentally bound, dialled, and published in this environment | **No** — QUIC listen addresses are rejected at startup | No | N/A | N/A | N/A |
| WebSocket | Module present, **`websockets` package missing** | No | No | N/A | N/A | N/A |
| WebRTC | Module present, **`aiortc` package missing** | No | No | N/A | N/A | N/A |
| WebTransport | Depends on the TLS/QUIC stack | No (`enable_webtransport` not passed) | No | N/A | N/A | N/A |

**Verified library behaviour that matters (see §15.4):**

- `new_host()` without `listen_addrs` yields a TCP transport; listeners are only
  started by `host.run(listen_addrs=...)`.
- `host.get_addrs()` returns the bound listener address verbatim, including
  `/ip4/0.0.0.0/...`; it does **not** normalize wildcards. Before Phase 1 the app
  therefore advertised `0.0.0.0` only because it hand-built a different string.
- `host.run()` does **not** raise when a listener fails to bind (for example a
  non-local `--listen-addr`). It silently starts zero listeners. Phase 1 added
  `_verify_listeners()`, which turns that into a startup failure.
- `get_addrs()` appends `/p2p/<peer-id>` and applies `announce_addrs` /
  `addrs_factory` at read time. Observed addresses recorded by Identify are
  merged into the same list by the library.
- The library does not add observed addresses for a purely outbound loopback
  connection to a peer listening on wildcard; advertised addresses stayed
  listener-derived in every Phase 1 run.

---

## 6. Address discovery and advertisement

Phase 1 separates three concepts that were previously conflated:

| Concept | Meaning | Source of truth |
|---|---|---|
| **Bind (listen)** | Addresses the local sockets bind | `--listen-addr`, else `/ip4/0.0.0.0/tcp/<port>` |
| **Observed** | Addresses remote peers report back via Identify | py-libp2p `ObservedAddrManager`, merged into `get_addrs()` |
| **Advertised (announce)** | Addresses this peer shares with others | `--announce-addr` verbatim, else `host.get_addrs()` filtered through an `AddrsFactory` that rewrites wildcard binds to `--announce-host` |

### 6.1 Verified address flow (after Phase 1)

- **Bind address:** `--listen-addr` (repeatable) or
  `/ip4/0.0.0.0/tcp/<port>` (`headless.py:_resolve_listen_addrs`).
- **Port default:** `HeadlessService.__init__` converts `0` to `4001`
  (`headless.py`). `DEFAULT_PORT = 9095` still exists but is unused. README
  examples remain inconsistent.
- **Advertised address:** derived at runtime from `host.get_addrs()` after
  listeners start (`_refresh_advertised_addrs`). Wildcard binds are rewritten to
  `announce_host` (default `127.0.0.1`) by `_make_addrs_factory`; concrete binds
  are advertised as bound. `--announce-addr` overrides both by passing
  `announce_addrs` to `new_host`.
- **Wildcard protection:** `announce_host` cannot be `0.0.0.0`/`::`, and
  `--announce-addr` values with wildcard hosts are rejected at startup.
- **Observed addresses:** Identify is registered and remote `observed_addr`
  values are recorded by the library and merged into `get_addrs()`. They are not
  persisted and do not replace the factory result for local listeners.
- **host.get_addrs():** now the single source of truth for announcement. The
  hand-built `/ip4/<host>/tcp/<port>/p2p/<id>` string is gone.
- **Wildcard dial normalization:** dial strings containing `/ip4/0.0.0.0/` or
  `/ip6/::/` are still rewritten to loopback before dialing
  (`_normalize_dial_addr`), so legacy shared addresses remain usable
  same-machine.

### 6.2 Consequences

- A peer started with defaults **still advertises loopback** (deliberate
  backward compatibility), but now logs a warning naming the dialability
  category: `loopback (same-machine only)`.
- LAN use requires `--announce-host <LAN IP>` (verified working) or
  `--announce-addr <multiaddr>`.
- There is still no public-IP discovery, NAT traversal, relay, or automatic
  observed-address promotion. A public deployment must supply an explicit
  announce address.
- There is still no Docker/container handling. A container that advertises its
  internal IP is not reachable from the host or LAN; Phase 7 must address this.
  Phase 1 records it as an explicit exception (see §15.5).
- IPv6 loopback now works when requested via `--listen-addr`; IPv6 wildcard and
  dual-stack behaviour are unit-tested in the factory but not exercised at
  runtime.
- Listener readiness is now observed, not assumed: `_verify_listeners()` reads
  `host.get_transport_addrs()` and fails startup when a requested address did
  not bind.

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
| Private peer identity | `_run_service` | `DataPaths.identity_file` (`<data-dir>/identity/peer.key`, mode 0600) | **Yes** | Only by deleting the key | Key file is never logged | Created once on first run; an explicit `--seed` is deterministic per run and never written |
| Peer ID | libp2p host | Derived from the persisted key | **Yes** | No | No | Stable across restarts; changes only if the key is deleted or `--seed` is used |
| Chat messages | `app/storage` message repository | SQLite at `<data-dir>/history.db` (override with `--storage-path`, `:memory:` for ephemeral) | **Yes** | No | Maybe (message content) | Capped at 1000/topic (`MAX_TOPIC_MESSAGES`) |
| Unread counts | message repository (`read` flag) | Derived from stored rows | **Yes** | No | No | Recomputed from rows, so retention cannot desynchronise it |
| Read flags | message repository (`read` flag) | SQLite | **Yes** | No | No | |
| Shared file metadata | `shared_files` | In-memory dict | **No** | No | Local paths held server-side | API hides `filepath` |
| File blocks | Bitswap `MemoryBlockStore` | In-memory | **No** | No (unless re-shared from disk) | Maybe | Large files consume RAM |
| Shared/downloaded files | `~/Downloads` | Filesystem | Yes | N/A | Maybe | Not under app data dir |
| Identify cache | `peer_info_cache` | In-memory | **No** | Yes by reconnecting | Public keys (not private) | 5-minute freshness check |
| DHT routing table | KadDHT | In-memory | **No** | Yes from network | No | Rebuilt via random walk/bootstrap |
| DHT application records | — | None | N/A | N/A | N/A | App does not store DHT records |
| Pubsub mesh / connections | libp2p | In-memory | N/A | Yes | No | Ephemeral by design |
| Topic subscriptions | `ChatRoom.subscriptions` | In-memory | **No** | No | No | Only CLI `--topic` survives via config |
| System log | `chatroom.system_logger` | `<data-dir>/logs/system_messages.txt` | **Yes** | N/A | Possibly | Rotating: 1 MB × 3; configured at startup, never in CWD |
| Application settings | CLI args + `.env` | CWD `.env` for Groq key | Yes if `.env` kept | N/A | `GROQ_API_KEY` | `UC_DATA_DIR` selects the data root; still no config file |
| RAG vector store | `rag_handler` | Package-relative `llm/codes/libp2p_vectorstore` | Read-only | Rebuildable | No | Path is inside source tree |
| API credentials | Groq SDK | Environment | Depends on env | N/A | **Yes** | Never log it |

### 7.1 Writable-path policy (Phase 4)

One data root, resolved at startup by `app/paths.py` in this order:
`--data-dir` → `UC_DATA_DIR` → `platformdirs.user_data_dir("py-peer",
"universal-connectivity")`. Relative values resolve against the CWD; `~` is

expanded. The runtime then creates the layout and writes a probe file before
opening anything, so a bad path fails startup with one clear error.

- Identity → `<data-dir>/identity/peer.key`, created once with mode 0600 and
  written through a temp file plus rename (§11.1, §18).
- Message history → `<data-dir>/history.db` (WAL, schema v1). `--storage-path`
  overrides just the database; `:memory:` keeps it ephemeral.
- System log → `<data-dir>/logs/system_messages.txt`, attached at runtime
  startup (`chatroom.configure_system_log`), so importing `chatroom` no longer
  creates a file in CWD.
- `.env` → current working directory (`main.py:17-18`) for `GROQ_API_KEY`.
- Downloads/uploads → `~/Downloads` (`headless.py:58,162-163`;
  `api/files.py:178-188`). Deliberately user-visible and already outside CWD;
  unchanged by Phase 4.
- Package source directory is used for the RAG vector store path
  (`rag_handler.py:21-23`), but it is read-only at runtime.

### 7.2 Delivery semantics

GossipSub is used as a live pub/sub transport only. A message is stored in
memory when it is observed on a subscribed topic; there is no acknowledgement,
durable queue, offline delivery, or synchronization mechanism. Any claim of
persistent chat history or offline delivery would be false today.

---

## 8. Kivy and dependency audit

### 8.1 Kivy usage (removed)

Phase 5 removed the Kivy UI entirely (§19). The historical observations below
record what existed before removal:

- Kivy was imported only by `kivy_ui.py`; `main.py` imported it lazily under
  `--kivy`.
- Core modules (`headless.py`, `chatroom.py`, `app/*`, `api/*`,
  `tornado_server.py`, `ui.py`, `main.py`) never imported Kivy.
- The Kivy UI talked to the same in-process service through direct calls and
  janus queues, like the Textual UI.
- No Buildozer, `.spec`, python-for-android, or Android/iOS packaging files were
  present, and none were added by removal.

Current state: no `kivy_ui.py`, no Kivy screenshot, no `--kivy` flag, and no
Kivy/KivyMD dependency or optional extra.

### 8.2 Dependency impact

Direct runtime dependencies (`pyproject.toml`):

```text
textual, libp2p (fork via uv.sources), trio, base58, protobuf, janus,
trio_asyncio, python-dotenv, platformdirs, groq, tornado
```

There are no optional dependency groups. Phase 5 removed `kivy`, `kivymd`,
`asynckivy`, `materialshapes`, `materialyoucolor`, Pillow, and the Windows
Kivy shims from both `pyproject.toml` and `uv.lock`.

Verified installed versions in `.venv`:

```text
libp2p 0.8.1 (fork branch fix/peerstore-local-identity)
textual 3.5.0
trio 0.30.0
trio_asyncio 0.16.0
janus 2.0.0
tornado 6.5.10
protobuf 6.33.6
```


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

Verified absent at the end of Phase 0; **Phase 1 closed the first three** with
`tests/test_address_config.py` (25 tests, including two real hosts that connect
and publish) and `tests/test_headless_lifecycle.py` (lifecycle coverage added
before Phase 1). Remaining gaps:

- No test covers the Tornado API handlers end-to-end.
- No test covers the Textual adapter or React UI.
- No test covers identity persistence (there is none).
- No test covers data-directory behaviour (there is none; Phase 4).
  *Phase 3 update: `tests/test_storage.py` covers persistence, dedupe,
  retention, schema migrations, backup-safe reopen, and engine integration.*
- No test covers signal delivery (the lifecycle tests cover `request_stop()`).
- No test covers packaging contents or installed console-script behaviour.
- No py-peer CI workflow runs any of these checks.

The existing tests are useful regression tests for bounded history, queue
behaviour, path containment, topic filtering, startup failure, JSON validation,
lifecycle teardown, and address configuration.

---

## 11. Risks and constraints for the refactor

### 11.1 Identity migration risk (highest)

Before Phase 4 the peer ID was a pure function of `--seed`, defaulting to the
shared literal `"py-peer"` — every default peer had the same ID. Phase 4 chose
**explicit opt-in** (option 2 of the brief) and documented it:

1. An explicit `--seed` still yields a deterministic in-memory identity for
   development and testing, and is never written to disk.
2. Without `--seed`, the first run generates one random Ed25519 key and stores
   it at `<data-dir>/identity/peer.key` (0600); later runs load it, so the peer
   ID is stable across restarts.

Consequence: a user who previously relied on the implicit default seed gets a
different peer ID once, and must re-share their multiaddr. This is deliberate
(the shared default produced ID collisions) and recorded in §18; it is not a
silent regeneration — the key is created once and then never rewritten. The
seed value is no longer logged, and a corrupt key file fails startup instead of
silently minting a new identity.

### 11.2 Address and deployment risk

Changing advertised addresses affects dialability. The current default is
loopback, so the existing "working" local demos do not prove LAN/remote
behaviour.

Phase 1 update: listener verification, wildcard rewriting, explicit announce
addresses, LAN loopback→private announcement, and two-peer TCP dial/publish were
verified (§15). Residual risk:

- The default remains loopback (compatibility decision), so packaging/docs must
  tell LAN users to pass `--announce-host` or `--announce-addr`.
- Container/NAT/public reachability is still untested; Docker is Phase 7.
- No automated NAT/remote test exists; only same-machine and same-LAN. The LAN
  test was a manual runtime check from a second process on the same host using
  its own LAN IP, not a second physical machine.
- QUIC/WebSocket/WebRTC/WebTransport remain disabled, so deployments that
  expect non-TCP transports are not supported yet.

### 11.3 Storage migration risk

Resolved by Phase 3 (§17): messages have a stable identity, dedupe, a per-topic
retention bound, and a versioned SQLite schema. There is no existing database to
migrate, so migration risk is limited to refusing a database written by a newer
build.

Storage remains local retention only. It must not be described as network-wide
offline delivery: there is still no acknowledgement, mailbox, or sync.

### 11.4 Lifecycle risk

Resolved by the §3.8 remediation:

- TUI exits now request stop and join the trio thread (verified). The
  service reports `ready=False, running=False` after `main()` returns.
- Headless and API modes handle SIGINT/SIGTERM gracefully (verified).
- Simple CLI exits cleanly after `quit` (verified).
- API `/service/stop` now goes through `request_stop()` and the single teardown
  path, so ChatRoom, Bitswap, and queues are closed (verified).
- API adapter startup failure after the peer is ready now stops the peer
  (verified: bind conflict exits 1 with a free peer port).
- A double-start guard exists.

Still open:

- Textual SIGTERM during an active UI and SIGTERM while the simple CLI is
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
- README previously described Kivy as mobile-ready; Phase 5 removed that claim
  along with the UI. No Android packaging or CI exists.

---

## 12. Recommended migration sequence (file-level)

This follows the brief's phase order. Do not write Docker first.

### Phase 1 — Transport/address correctness (done — see §15)

**Goal:** prove TCP listener and inbound/outbound behaviour, then fix
announcement.

Delivered:

- `headless.py`: `listen_addrs`/`announce_addrs` constructor inputs,
  `_resolve_listen_addrs`, `_resolve_announce_addrs`, `_make_addrs_factory`,
  `_verify_listeners`, `_refresh_advertised_addrs`, address classification, and
  per-address port probing.
- `main.py`: repeatable `--listen-addr` / `--announce-addr`, wired into all four
  `HeadlessService` construction sites.
- `api/node.py`: `/node/info` and `/node/multiaddr` expose the advertised list
  and bound listen addresses.
- `tests/test_address_config.py`: 25 tests (parsing, factory, wildcard
  rejection, listener verification, two-peer connect/publish).

Decisions: default `announce_host` stays `127.0.0.1` with a warning; wildcard
listeners are rewritten; concrete listeners are advertised as bound; explicit
`--announce-addr` wins. Docker/external connectivity is deferred to Phase 7 as
an explicit exception (§15.5).

### Phase 2 — Unified service layer and shared runtime (done — see §16)

Likely touch points:

- New runtime/service modules (for example `runtime.py`, `services/`), or a
  clearly named package.
- `headless.py`: split host/runtime ownership from API-facing query methods.
- `api/*.py`: call services, not `HeadlessService` internals.
- `ui.py`, `main.py`: inject services instead of the concrete glue object.
- Tests: service-level tests with fake/in-memory dependencies; one operation
  exercised directly and through the HTTP adapter.

### Phase 3 — Storage design (done — see §17)

Define message/peer/settings repositories, message identity/dedupe, retention,
and migration policy. Do not add a server database. Prefer SQLite with
non-blocking access, versioned migrations, and backup/restore docs.

### Phase 4 — Persistence and data directories (done — see §18)

Delivered:

- `app/paths.py`: `DataPaths.resolve` (`--data-dir` → `UC_DATA_DIR` →
  platformdirs) plus `ensure_writable()`, which creates the layout and probes
  it, raising `DataDirectoryError` with an actionable message.
- Identity: the first unflagged run generates one random Ed25519 key and saves
  it to `<data-dir>/identity/peer.key` (0600, temp file + rename); later runs
  load it. An explicit `--seed` stays deterministic and is not persisted.
- `system_messages.txt` moved to `<data-dir>/logs/` and is attached at runtime
  startup instead of at import; the database defaults to `<data-dir>/history.db`.
- Tests: `tests/test_paths.py` (29 tests) covers precedence, layout,
  unwritable directories, identity create/reuse/corruption, log placement, and
  runtime wiring. A two-process API run verified a stable peer ID and history
  across a restart.

### Phase 5 — Remove Kivy from the runtime (done — see §19)

Delivered:

- Deleted `kivy_ui.py`, the Kivy screenshot, `--kivy`, the optional `kivy`
  extra, and every Kivy/KivyMD lock entry.
- `RuntimeConfig.from_namespace` derives `ui_mode` only from `--ui`;
  `main.py` has no Kivy branch or deferred import.
- Replaced `tests/test_kivy_optional.py` with `tests/test_kivy_removed.py`,
  which asserts the module, flag, dependency, extra, asset, and source
  references are gone and that core modules import with Kivy blocked.
- README documents only the Textual TUI, simple CLI, API, and headless modes;
  Android remains an explicit, unstarted decision.

### Phase 6 — UI adapters (done — see §20)

Delivered: Textual and CLI consume the runtime EventBus; Textual imports
lazily; the API binds loopback by default with an explicit trust boundary;
React retries REST/WebSockets; adapter and API-security tests were added.

### Phase 7 — Docker/Compose (done — see §21)

Delivered: multi-stage locked build, unprivileged runtime, `/data` volume,
readiness-based health check, Compose service with only TCP 4001/8765, graceful
SIGTERM, persistence across recreation, and documented address/network
caveats.

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
   *Phase 1 answer: `get_addrs()` returns `/ip4/0.0.0.0/...` verbatim (no
   normalization); observed addresses were not added after a loopback
   connection. Phase 1 rewrites wildcards through an `AddrsFactory`. NAT
   behaviour remains unknown (§15.5).*
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
- [x] Startup and shutdown paths documented for API/TUI/CLI/headless (Kivy was
      removed in Phase 5).
- [x] Transport and advertised-address logic traced (TCP-only; manual
      announcement; default loopback).
- [x] Persistence status documented for each data category.
- [x] Kivy usage and dependency relationships documented (the UI was removed in
      Phase 5).
- [x] Baseline test results recorded (25 pass, ruff 83 errors, frontend
      lint/build, package build).
- [x] Unknowns explicitly listed rather than guessed.

**Phase 0 conclusion:** the repository is understood well enough to begin
Phase 1, with two blocking decisions recorded above: identity migration policy
and the supported network/deployment model. No application code was changed in
this phase; the only addition is this audit document.

---

## 15. Phase 1 — Transport audit and announced-address correctness (implemented)

**Status:** implemented and verified on 2026-11-08. All commands below were run
against the working tree; nothing in this section is inferred.

### 15.1 Transport inventory (the brief's ten questions)

| Question | TCP/IPv4 | QUIC | WebSocket | WebRTC | WebTransport |
|---|---|---|---|---|---|
| Available in installed py-libp2p | Yes | Yes, `aioquic 1.7.0` installed | Module ships, **`websockets` not installed** | Module ships, **`aiortc` not installed** | Module ships; needs TLS/QUIC stack |
| Imported/registered with host | Yes (auto-detected from `listen_addrs`) | No (app rejects QUIC listen addresses) | No | No | No |
| Enabled by configuration | Yes, default | No | No | No | No |
| Host starts a listener | Yes, verified | No (experimentally yes, §15.4) | No | No | No |
| Local bind | `--listen-addr`, default `/ip4/0.0.0.0/tcp/<port>` | N/A | N/A | N/A | N/A |
| Accepts inbound | Yes, verified (§15.4) | Experimentally yes | N/A | N/A | N/A |
| Can dial | Yes, verified (§15.4) | Experimentally yes | N/A | N/A | N/A |
| Advertised | Yes, via `get_addrs()` + factory | N/A | N/A | N/A | N/A |
| Works on macOS/Linux | Yes (macOS verified; Linux expected via the same socket path, not run in this phase) | Not supported | N/A | N/A | N/A |
| Known limitations | SO_REUSEPORT hides port conflicts; no NAT/public discovery | Deliberately disabled in Phase 1; enabling it changes the transport mix (a QUIC-only listen list would remove TCP dialing). Rejected with a clear startup error | Missing dependency; `pyproject.toml` does not declare it | Missing dependency; `aiortc` is heavy and not declared | Not enabled; certificate manager not configured |

Why the other transports were **not** enabled in this phase:

- **QUIC** — `aioquic` is installed, and an experiment (two peers, QUIC-only
  listen addresses) successfully bound `/ip4/.../udp/<p>/quic-v1`, advertised it,
  connected, and delivered a GossipSub message (§15.4, scenario 11). It is not
  enabled because Phase 1 is scoped to TCP/address correctness and because
  py-libp2p builds its transport list from the listen addresses: a QUIC-only
  listen list would silently remove TCP dialing, which the bootstrap path still
  needs. `--listen-addr .../quic-v1` is therefore rejected with an explicit
  error rather than half-supported.
- **WebSocket** — the transport module imports `websockets`, which is not in the
  environment or `pyproject.toml`. Enabling it would require a dependency change
  and browser/React integration testing.
- **WebRTC** — requires `aiortc` (not installed, not declared) plus signalling
  design; out of scope.
- **WebTransport** — requires TLS certificate management and an explicit
  `enable_webtransport` path; no consumer exists yet.

### 15.2 Address model implemented

```
--listen-addr (repeatable)         -> sockets bind here
   default: /ip4/0.0.0.0/tcp/<port>

host.get_transport_addrs()         -> what actually bound (may be 0.0.0.0)

--announce-addr (repeatable)       -> advertised verbatim (wins over factory)
   else AddrsFactory rewrites      -> wildcard bind becomes /ip4/<announce-host>/...
   else concrete bind              -> advertised as bound

host.get_addrs()                   -> advertised list + /p2p/<peer-id>
                                      (library also merges Identify-observed
                                       addresses into this list)
```

Rules enforced at startup:

- Listen addresses must parse and contain a plain TCP protocol. QUIC,
  WebSocket, WebRTC, and WebTransport listen addresses are rejected with an
  explicit "not enabled in this build" error (see §15.1 for why).
- `--announce-addr` / `announce_host` may not be `0.0.0.0`, `::`, or `::0`.
- A listener that fails to bind is a startup error
  (`RuntimeError: libp2p host did not start any listener`), not a silent
  warning.
- A listen port of 0 means "ephemeral": the requested address matches the
  port the OS actually assigned during listener verification.
- Every advertised address is classified and logged:
  `wildcard` / `loopback` / `link-local` / `private/LAN` / `public/unknown`.
  Loopback emits a warning with the fix (`--announce-host` / `--announce-addr`).

### 15.3 Code changes

| File | Change |
|---|---|
| `headless.py` | `listen_addrs`/`announce_addrs` inputs; `_resolve_listen_addrs`, `_resolve_announce_addrs`, `_make_addrs_factory`, `_verify_listeners`, `_refresh_advertised_addrs`, `_is_wildcard_addr`, `_classify_announced_addr`; per-address port probing with `EADDRINUSE`/`EADDRNOTAVAIL`; host creation passes `listen_addrs` (transport auto-detection), `announce_addrs`, and `addrs_factory`; `full_multiaddrs`/`transport_addrs`; `get_connection_info()` exposes both lists |
| `main.py` | repeatable `--listen-addr` / `--announce-addr`; wired into all four `HeadlessService` construction sites |
| `api/node.py` | `/api/v1/node/info` and `/api/v1/node/multiaddr` return `multiaddrs` and `listen_addrs` |
| `tests/test_address_config.py` | 25 tests: listen parsing/validation, factory rewrite/dedupe/family handling, announce validation, wildcard detection, classification, listener verification, default/explicit/announce integration, two-peer connect + publish |

Behavior intentionally preserved:

- Default port remains `4001` (`0` maps to `4001`).
- Default `announce_host` remains `127.0.0.1`; local workflows are unchanged.
- TCP remains the only enabled transport.
- Wildcard dial normalization (`_normalize_dial_addr`) still accepts legacy
  `/ip4/0.0.0.0/...` strings.

### 15.4 Connectivity test matrix (executed)

| # | Scenario | Configuration | Expected | Result |
|---|---|---|---|---|
| 1 | Default startup | no flags | bind `0.0.0.0`, advertise `127.0.0.1` + warning | **PASS** — `Listener(s) active: /ip4/0.0.0.0/tcp/<p>`; advertised `/ip4/127.0.0.1/tcp/<p>/p2p/<id>`; class `loopback (same-machine only)` |
| 2 | Same-machine two peers | two ports, seeds A/B | connect + publish/receive | **PASS** — both connected; B received `hello-phase1` / `phase1-after` |
| 3 | Explicit concrete loopback bind | `--listen-addr /ip4/127.0.0.1/tcp/<p>` | bind and advertise that exact address | **PASS** — no rewrite; advertised `/ip4/127.0.0.1/...` |
| 4 | Explicit announce | `--announce-addr /ip4/127.0.0.1/tcp/<p>` | advertised verbatim | **PASS** — `full_multiaddrs == [<announce>/p2p/<id>]` |
| 5 | LAN announcement | `announce_host=10.96.120.90` | advertise LAN IP; dial from another local process | **PASS** — class `private/LAN`; peer connected and received `lan-hello` |
| 6 | IPv6 loopback | `--listen-addr /ip6/::1/tcp/<p>` | bind and advertise IPv6 | **PASS** — `transport_addrs=['/ip6/::1/tcp/56126']`; advertised `/ip6/::1/tcp/56126/p2p/<id>` |
| 7 | Bad bind (non-local IP) | `--listen-addr /ip4/192.0.2.1/tcp/<p>` | startup failure, not a zombie | **PASS** — probe warns `not assigned`; `_verify_listeners` raises; service `failed=True`, thread exits |
| 8 | CLI plumbing | `--headless --listen-addr ... --announce-addr ...` | flags reach the service | **PASS** — log shows configured listen + explicit announce; listener active; SIGINT exits 0 |
| 9 | Docker / container | bridge network, published TCP 4001 | start, health, announce, shutdown | **PASS (Phase 7)** — see §21; container listener `/ip4/0.0.0.0/tcp/4001`, advertised `/ip4/<announce-host>/tcp/4001/p2p/<id>` |
| 10 | NAT / public internet | none | must be explicit exception | **NOT RUN** — see §15.5 |
| 11 | QUIC (experiment only) | `--listen-addr /ip4/127.0.0.1/udp/<p>/quic-v1` on two peers | bind, advertise, connect, publish | **PASS (experimental)** — `A transports: ['QUICTransport']`; advertised `/ip4/127.0.0.1/udp/52798/quic-v1/p2p/<id>`; connected; received `quic-hello`. Not exposed in the app because a QUIC-only listen list removes TCP dialing (§15.1) |
| 12 | Ephemeral port | `--listen-addr /ip4/127.0.0.1/tcp/0` | bind an OS-assigned port and advertise it | **PASS** — `transport_addrs=['/ip4/127.0.0.1/tcp/57444']`; advertised the assigned port |

Cross-machine LAN was not available. Test 5 used the host's own LAN IP from a
second process; it proves the announcement reaches a routable address, not that
a second physical machine can reach it.

### 15.5 Explicit exceptions and gaps

- **Docker/container connectivity was not tested in Phase 1**; Phase 7 later
  validated the bridge-network case (§21). Binding `0.0.0.0` inside a container
  still advertises a container-internal IP unless the operator sets
  `--announce-host` (or `--announce-addr`) to a host-reachable address. Phase 7
  supplies the port mapping and readiness health check; it does not add NAT
  correction or relay.
- **External/NAT/public reachability is not tested.** There is no UPnP, AutoNAT,
  relay, or public-IP discovery. Public deployments must set an explicit announce
  address and ensure the port is forwarded.
- **Observed-address promotion is untested behind NAT.** The library records
  Identify-observed addresses, but Phase 1 did not verify that a NATed peer can
  advertise its public address automatically (it is not expected to).
- **IPv6 wildcard and dual-stack** are unit-tested in the factory only; runtime
  was verified for `::1` only.
- **Cross-platform listener probing** covers IPv4 only; the port-in-use warning
  skips IPv6 addresses.
- **QUIC/WebSocket/WebRTC/WebTransport** remain disabled for the dependency and
  scope reasons in §15.1.

### 15.6 Verification commands and results

```text
.venv/bin/python -m unittest tests.test_address_config -v
  -> Ran 25 tests ... OK

.venv/bin/python -m unittest discover -s tests
  -> Ran 54 tests in 1.086s ... OK

.venv/bin/python -m py_compile headless.py main.py
  -> COMPILE_OK

.venv/bin/python main.py --help
  -> lists --listen-addr, --announce-addr, --announce-host

.venv/bin/python main.py --headless --nick cli --port <p> \
    --listen-addr /ip4/127.0.0.1/tcp/<p> \
    --announce-addr /ip4/127.0.0.1/tcp/<p>
  -> Configured listen address(es): /ip4/127.0.0.1/tcp/<p>
  -> Using explicit announce address(es): /ip4/127.0.0.1/tcp/<p>
  -> Listener(s) active: /ip4/127.0.0.1/tcp/<p>
  -> exit 0 on SIGINT
```

Runtime scenarios 1–8 are recorded in §15.4 with their observed log lines.

### 15.7 Acceptance criteria (brief §5.1–5.4)

- [x] Transport inventory records availability, registration, configuration,
      listener, inbound, outbound, advertisement, platforms, and limitations.
- [x] Bind, observed, and advertised addresses are defined and handled
      separately.
- [x] Listen and advertise addresses are configurable from the CLI.
- [x] Listener startup is verified rather than assumed, and failure is fatal.
- [x] Same-machine connectivity is tested (two real hosts, publish/receive).
- [x] LAN-style connectivity is tested with a private address.
- [x] Docker/NAT gaps were documented; Phase 7 validated Docker bridge
      networking and host-reachable announcement (§21). NAT traversal remains
      out of scope.
- [x] Address decisions are logged at startup and exposed through the API.

**Phase 1 conclusion:** TCP transport and announced-address correctness are
verified for same-machine and private-LAN use. The default remains loopback for
backward compatibility and warns loudly. Container, NAT, and non-TCP transports
are deliberately out of scope and recorded as exceptions rather than claimed as
working.

## 16. Phase 2 — Unified service layer and shared runtime (implemented)

**Status:** implemented and verified, 2026-11-08.
**Baseline:** `a06da54` (Phase 1) plus the working tree described here.
**Scope:** one runtime, one libp2p host, one service layer, adapters as translators.

### 16.1 Package layout

| Module | Responsibility |
|---|---|
| `app/runtime.py` | `ApplicationRuntime`: owns the one `HeadlessService`/host, constructs services, attaches the event bus, runs and joins the trio thread |
| `app/errors.py` | `ApplicationError` hierarchy carrying `code` and `http_status` |
| `app/models.py` | `PeerIdentity`, `ConnectionSummary`, `ServiceStatus`, `RuntimeConfig` |
| `app/events.py` | `Event`, `EventType`, `EventBus`, `EventSubscription` (bounded janus queues) |
| `app/services/peer.py` | identity, connection info, peerstore detail, advertised/listen addresses |
| `app/services/connection.py` | multiaddr validation, queued dials, connection checks |
| `app/services/messaging.py` | publish to default/topic, history, unread, mark-read |
| `app/services/discovery.py` | topics, mesh/fanout, pubsub, DHT queries, bootstrap |
| `app/services/identity.py` | cached identify metadata and public keys (never private keys) |
| `app/services/files.py` | share/download/upload with download-directory containment |
| `app/services/storage.py` | paginated history over the storage repository |
| `app/storage/` | message repository (memory/SQLite), peer + settings repositories, schema v1 migrations |

No module under `app/` imports Tornado, Textual, Kivy, or MCP. Services return
dataclasses/dicts and raise `ApplicationError` subclasses.

### 16.2 Runtime lifecycle

- `ApplicationRuntime(config)` builds `HeadlessService` once and exposes
  `peer`, `connections`, `messaging`, `discovery`, `identity`, `files`,
  `storage` over that same engine.
- `start()` is single-use (`RuntimeError` on a second call) and awaits
  `HeadlessService.start()` on the owning trio loop.
- `start_in_thread(timeout)` waits for `ready`/`failed`, and on failure tears
  down partially started resources before raising.
- `request_stop()` is thread-safe and only signals; teardown happens once on
  the trio thread in `HeadlessService.start()`'s `finally` block.
- `join()` waits for the runtime thread; `close()` closes event subscriptions.
- `status()` and `config_snapshot()` provide adapter-facing health/config
  without exposing engine internals.
- `api.base.as_runtime()` wraps a bare service once and caches it on the
  service (`_uc_runtime`), so every handler and WebSocket in a process shares
  the same runtime, services, and event bus.
- `main.py` builds exactly one runtime in headless, API, and Textual modes;
  startup failure surfaces as a non-zero exit. (The former Kivy mode was removed
  in Phase 5.)

### 16.3 Errors at the adapter boundary

`BaseHandler._handle_request_exception` maps service errors onto the existing
JSON envelope: `ValidationError` → 400, `NotFoundError` → 404,
`ConflictError` → 409, `NotReadyError`/`OperationError` → 503,
`UnsupportedOperationError` → 501, other `ApplicationError` → 500. Unknown
exceptions still use Tornado's `write_error`, with tracebacks kept in the log.

### 16.4 Events and backpressure

`EventBus.publish` is non-blocking and never raises into the networking loop:
each subscriber has a bounded `janus` queue (256 events by default), a full
queue drops its oldest event and increments `dropped`, and subscribers may
filter by `EventType`. `HeadlessService` publishes `message_received`,
`system`, `peer_connected`, `peer_disconnected`, and `topic_subscribed`
without affecting legacy queue delivery. `/ws/messages` and `/ws/system`
consume the bus and unsubscribe on close (legacy queues remain a fallback for
engines without a bus); `/ws/peers` and `/ws/pubsub/mesh` read through
`runtime.peer` / `runtime.discovery`.

### 16.5 Adapter coverage

| Adapter | Path to application behaviour |
|---|---|
| Tornado REST (`/api/v1/*`) | `BaseHandler` → runtime services; no handler reads libp2p state directly |
| Tornado WebSockets (`/ws/*`) | runtime services + event bus subscriptions |
| Textual TUI (`ui.py`) | `runtime.messaging.send`, `runtime.peer.connection_info()` |
| Simple CLI/interactive (`main.py`) | runtime services via `run_simple_interactive` |

The Kivy adapter was removed in Phase 5 (§19), so there is no GUI adapter left
to migrate.

### 16.6 Verification

```
.venv/bin/python -m unittest discover -s tests
  -> Ran 82 tests in ~1.1s ... OK
     (54 pre-existing + 28 new in tests/test_services.py and tests/test_runtime.py)

.venv/bin/python -m py_compile main.py tornado_server.py ui.py \
    api/*.py app/*.py app/services/*.py tests/test_services.py tests/test_runtime.py
  -> COMPILE_OK
```

API smoke run (`main.py --api` with ephemeral peer/API ports):

```
GET  /api/v1/node/status      -> ready true, running true, nickname "smoke"
GET  /api/v1/node/info        -> advertised /ip4/127.0.0.1/tcp/<p>/p2p/<id>
GET  /api/v1/service/status   -> state "ready", failed false, peer_count 0
GET  /api/v1/service/config   -> listen_addrs, announce_addrs, download_dir
GET  /api/v1/topics           -> 1 topic (universal-connectivity)
GET  /api/v1/peers/count      -> 0
POST /api/v1/messages         -> 202 queued
POST /api/v1/messages {"message": 123} -> 400 (service validation)
POST /api/v1/service/stop     -> graceful shutdown, process exit 0
```

### 16.7 Acceptance criteria (brief §6.5)

- [x] Services contain application functionality and import no HTTP/Textual/Kivy/MCP framework.
- [x] HTTP handlers call services instead of implementing networking logic.
- [x] Textual actions call the same services.
- [x] A future MCP adapter can call those services without duplicating functionality.
- [x] One runtime owns one peer host per application instance.
- [x] Startup and shutdown are explicit and tested.
- [x] Partial startup failure cleans up initialized resources.
- [x] Service tests run without starting a real UI.
- [x] Event consumers cannot block the networking runtime (bounded, drop-oldest, non-raising publish).

### 16.8 Explicit exceptions and follow-ups

- **Kivy** was removed in Phase 5 (§19). No GUI adapter remains; Textual, CLI,
  and HTTP are the supported interfaces.
- **StorageService** is a read-only facade over the repository. Phase 3 added
  durable writes, identity/dedupe, and migrations (§17); Phase 4 made the
  default database `<data-dir>/history.db` (§18).
- **ConnectionService.connect** only queues a dial (unchanged semantics);
  explicit disconnect is not implemented because no existing caller needs it.
- **API trust boundary** remains local/unauthenticated; authorization belongs
  with the future remote/MCP adapter.
- Non-TCP transports stay disabled per §15.

## 17. Phase 3 — Storage design (implemented)

**Status:** implemented and verified, 2026-11-08.
**Baseline:** `7678a69` (Phase 2) plus the working tree described here.
**Scope:** one local message repository, stable message identity, dedupe,
bounded retention, versioned schema, backup/restore guidance. No server
database and no offline delivery.

### 17.1 Package layout

| Module | Responsibility |
|---|---|
| `app/storage/__init__.py` | `open_store(path)`, `MemoryStore`, `SqliteStore`, health, connection lifecycle |
| `app/storage/messages.py` | `message_identity`, normalisation, `InMemoryMessageStore`, `SqliteMessageStore` |
| `app/storage/repositories.py` | peer metadata and settings repositories (memory/SQLite); schema is ready, services consume them in Phase 4 |
| `app/storage/schema.py` | `SCHEMA_VERSION` and ordered migrations via `PRAGMA user_version` |

`HeadlessService` now stores through `message_store` instead of owning
`topic_messages`/`topic_unread_counts`. `ApplicationRuntime` constructs the
store once and injects the same instance into the engine and `StorageService`.

### 17.2 Backend selection

| Mode | Flag | Backend | Survives restart |
|---|---|---|---|
| Default | none | in-memory (`MemoryStore`) | No — behaviour unchanged from Phase 2 |
| Durable | `--storage-path PATH` | SQLite file (`SqliteStore`) | Yes |
| Ephemeral SQLite | `--storage-path :memory:` | SQLite, process-lifetime | No |

`open_store(path)` creates parent directories, opens the database, applies
pending migrations, and fails with `StorageError` (startup error, non-zero
exit) when the path cannot be opened or migrated. The store is closed in
`ApplicationRuntime.start()`'s `finally` block after the engine stops;
`close()` is idempotent.

### 17.3 Message identity and dedupe

- Identity is `sha256(topic \x00 sender_id \x00 timestamp \x00 content)`.
  An explicit `message_id` on the input is preserved instead.
- The id is the primary key, so a redelivered GossipSub frame is rejected by
  `INSERT OR IGNORE`. Duplicates are dropped before UI/WebSocket fan-out, so a
  message is stored, queued, and streamed once.
- The same text sent at a different timestamp is a different message.
- Own messages (the GossipSub echo) are stored with `read=1`, so they never
  increment the local unread badge.
- The id is included in the stored row, API history responses, and WebSocket
  payloads, which gives clients a stable key for de-duplication.

### 17.4 Retention

- Per-topic cap: `MAX_TOPIC_MESSAGES` (1000) enforced by the engine on every
  append; the repository also has a default cap validated to 1–100000.
- Trimming deletes the oldest rows in the same transaction as the insert.
- Unread counts are computed from stored rows (`read = 0`), so trimming cannot
  desynchronise the badge from the history.
- Retention is count-based only. There is no age-based expiry, export, or
  per-topic override yet.

### 17.5 Schema and migrations

Schema v1 (`PRAGMA user_version = 1`):

- `messages(id PK, topic, sender_id, sender_nick, content, kind, timestamp,
  received_at, read, file_cid, file_name, file_size)` with an index on `topic`.
- `peers(peer_id PK, public_key, protocol_version, agent_version, protocols,
  listen_addrs, updated_at)`.
- `settings(key PK, value, updated_at)`.

Migrations are applied in order inside a transaction. A database whose
`user_version` is newer than the build's `SCHEMA_VERSION` is refused with a
clear error instead of being modified. There is no down-migration: restoring a
backup is the rollback path.

### 17.6 Concurrency and failure behaviour

- One SQLite connection, guarded by an `RLock`, opened with
  `check_same_thread=False`; WAL, `synchronous=NORMAL`, and a 5 s busy timeout
  keep the Tornado reader thread from blocking the writer.
- Durable appends run on a worker thread (`trio.to_thread.run_sync`), so disk
  latency never blocks the networking loop. In-memory appends stay inline.
- A storage failure is logged and reported as a system message; the live
  message is still delivered to the UI/WebSocket clients and the node keeps
  running.
- No transaction spans a network operation; each append is one INSERT plus one
  retention DELETE.

### 17.7 Delivery semantics (unchanged)

Storage is **local retention of messages this peer observed**. It is not an
acknowledgement, mailbox, offline delivery, or multi-device sync. A peer that
was offline never receives missed messages by starting with a database. Do not
describe the feature as "offline chat history".

### 17.8 Backup and restore

Stop the peer, then copy the file (WAL means a live copy must include the
`-wal`/`-shm` siblings, or use the safe online backup):

```bash
# preferred: consistent copy even while running
sqlite3 data/history.db ".backup 'backup.db'"

# or with the peer stopped
cp data/history.db backup.db

# inspect
sqlite3 data/history.db "SELECT COUNT(*) FROM messages;"
sqlite3 data/history.db "PRAGMA user_version;"
```

Restore by stopping the peer, replacing the database file, and restarting.
Restoring an older `user_version` is safe: pending migrations run on open.
Restoring a newer `user_version` is refused, which protects the file from a
downgrade.

### 17.9 Configuration and API surface

- `--storage-path PATH` (all modes) overrides the database location; since
  Phase 4 the default is `<data-dir>/history.db`, and `:memory:` is ephemeral.
- `GET /api/v1/service/config` includes a `storage` block:
  `{backend, durable, schema_version, retention_per_topic, messages,
  peer_records, settings}` (Phase 4 adds `data_dir`, `identity_file`, and
  `identity_source`).
- Stored messages and history responses include `message_id`.

### 17.10 Verification

```
.venv/bin/python -m unittest discover -s tests
  -> Ran 111 tests ... OK
     (82 after Phase 2 + 29 new in tests/test_storage.py)

.venv/bin/python -m py_compile main.py headless.py app/storage/*.py \
    app/services/storage.py app/runtime.py app/models.py tests/test_storage.py
  -> COMPILE_OK
```

Two-process API run with the same `--storage-path` (send a message, stop,
restart, read history) verified that the message and its `message_id` survive a
restart and that a duplicate frame is stored once.

### 17.11 Acceptance and explicit exceptions

- [x] Message identity is stable and documented.
- [x] Redelivery is deduplicated before storage and fan-out.
- [x] Retention is bounded and enforced transactionally.
- [x] Storage works without a UI and without a server database.
- [x] Schema is versioned; newer databases are refused; backup/restore documented.
- [x] Service tests run against both backends without a real host.
- [x] Default data directory and `UC_DATA_DIR` land in Phase 4 (§18).
- [ ] Peer/settings repositories are implemented and tested but still not wired
  into services: the identify cache is written from the networking loop (a
  durable write there needs an off-loop flush and a cache-invalidation policy)
  and no user preference is defined well enough to persist yet. Deferred, not
  forgotten.
- [ ] No offline delivery, acknowledgement, sync, message edit/delete, full-text
  search, or age-based retention.
- The former Kivy adapter was removed in Phase 5 (§19); the Textual, CLI, and
  API adapters read the same repository through services.

## 18. Phase 4 — Persistence and data directories (implemented)

**Status:** implemented and verified, 2026-11-08.
**Baseline:** `fd123fb` (Phase 3).
**Scope:** one data root for identity, history, and logs; a persisted identity
with an explicit migration decision; a writability preflight. No new server
component and no schema change (`SCHEMA_VERSION` stays 1).

### 18.1 Layout and resolution

| Data | Location | Notes |
|---|---|---|
| Private identity | `<data-dir>/identity/peer.key` | 0600, created once |
| Message history | `<data-dir>/history.db` | SQLite, WAL, schema v1 |
| System log | `<data-dir>/logs/system_messages.txt` | rotating 1 MB × 3 |

`app.paths.DataPaths.resolve` picks the root once per process:

1. `--data-dir PATH`
2. `UC_DATA_DIR`
3. `platformdirs.user_data_dir("py-peer", "universal-connectivity")`

Relative values are anchored at the current working directory and `~` is
expanded. `--storage-path` still overrides only the database (so an existing
deployment keeps its history file where it was) and `:memory:` skips the
database directory entirely.

### 18.2 Writability preflight

`DataPaths.ensure_writable()` creates the root, `identity/`, and `logs/`, then
writes and removes a probe file in each required directory. Any `OSError`
becomes `DataDirectoryError` (`data_directory_error`, HTTP 500) with the path
and the `UC_DATA_DIR` / `--data-dir` remedy in the message. `ApplicationRuntime`
calls it before constructing the store or the service, so an unwritable data
directory fails the process at startup instead of failing a later key or SQLite
write. This satisfies the "unwritable dir fails safely" acceptance item.

### 18.3 Identity decision

The pre-Phase-4 identity was `sha256(--seed)` with the shared default
`"py-peer"`, i.e. every default peer had the same peer ID, and the seed was
logged at INFO. Phase 4 resolves §11.1 with explicit opt-in:

- `--seed` present → deterministic Ed25519 key, never written to disk. The
  value is not logged (only the fact that a seed was used), so development
  runs stay reproducible without leaking the input.
- `--seed` absent and `identity/peer.key` exists → load it. This is the normal
  restart path; the peer ID is stable even if a seed is later added or removed.
- `--seed` absent and no key → generate one Ed25519 key and persist it with
  `save_identity` into a temp file, `chmod 0600`, then `os.replace`, so a crash
  never leaves a half-written key.
- No `identity_path` (direct `HeadlessService` use in tests) → ephemeral key,
  logged as a warning because the peer ID will change on restart.
- Corrupt key file → startup fails with the path and the consequence (deleting
  the file creates a new identity and peer ID). The peer never silently mints
  a replacement identity.

Compatibility consequence, stated in the README: a user who relied on the
implicit default seed changes peer ID once and must re-share their multiaddr.
That is the deliberate cost of removing the global default-ID collision.

`GET /api/v1/service/config` reports `identity_source`
(`created` / `persisted` / `seed` / `ephemeral`) and `identity_file`, so
operators can confirm behaviour without reading the key.

### 18.4 System log and default storage

- `chatroom` no longer attaches a `RotatingFileHandler` at import. It exposes
  `configure_system_log(path)`, which replaces any existing rotating handler
  (idempotent), creates the parent directory, and falls back to a warning plus
  no file sink if the handler cannot be created. `ApplicationRuntime` calls it
  with `<data-dir>/logs/system_messages.txt`, so running from any directory no
  longer drops a log in CWD.
- `open_store` still receives `None` only when the caller wants memory storage.
  The runtime now passes `<data-dir>/history.db` by default, which makes history
  durable by default; `--storage-path :memory:` restores the ephemeral
  behaviour.

### 18.5 Concurrency and failure behaviour

The identity file is read or written once on the trio thread before the host is
created, so it never blocks message flow. `ensure_writable` runs on the
constructing thread before any host exists. `configuration_snapshot` reads plain
attributes and does not touch the key file. The store keeps its Phase 3 locking
and off-loop append behaviour.

### 18.6 Verification

```
.venv/bin/python -m unittest discover -s tests
  -> Ran 140 tests ... OK
     (111 after Phase 3 + 29 new in tests/test_paths.py)

.venv/bin/python -m py_compile app/paths.py app/errors.py app/models.py \
    app/runtime.py chatroom.py headless.py main.py
  -> COMPILE_OK
```

Two-process API run, both processes started from a scratch CWD with
`--data-dir <tmp>` and different ports:

```
run 1: identity_source "created",   peer_id 12D3KooWDeQ…Fce5, history [phase4-persisted], exit 0
run 2: identity_source "persisted", peer_id 12D3KooWDeQ…Fce5, history [phase4-persisted], exit 0
files: <data-dir>/{identity/peer.key (0600), history.db, logs/system_messages.txt}
CWD:   only the scratch process logs; no system_messages.txt, history.db, or identity/
```

### 18.7 Acceptance and explicit exceptions

- [x] Restart preserves the peer ID and message history.
- [x] An unwritable data directory fails startup with a clear, actionable error.
- [x] `UC_DATA_DIR` and `--data-dir` are documented with platform defaults.
- [x] Identity, history, and the system log no longer depend on the CWD.
- [x] The seed value is no longer logged.
- [ ] Downloads/uploads stay in `~/Downloads` (already outside CWD and
  user-visible); they are not moved under the data directory.
- [ ] Peer/settings repositories remain unwired for the reasons in §17.11.
- [ ] Two peers sharing one data directory share an identity and database; the
  README tells multi-peer users to pass `--data-dir` or `--seed`.
- [ ] The identity file is not encrypted at rest; protection is filesystem
  permissions (0600) and the data directory's own permissions.

## 19. Phase 5 — Remove Kivy from the runtime (implemented)

**Status:** implemented and verified, 2026-11-08.
**Baseline:** `0364296` (Phase 4).
**Scope:** retire the Kivy UI and remove every Kivy/KivyMD dependency, CLI
flag, screenshot, and adapter reference. Core network behaviour and the
service layer are unchanged.

### 19.1 What was removed

| Removed | Detail |
|---|---|
| `kivy_ui.py` | The entire Kivy/KivyMD adapter (1,374 lines). |
| `assets/images/py-peer-kivy.png` | Kivy screenshot asset. |
| `--kivy` flag | argparse no longer accepts it; the process exits 2 as for any unknown option. |
| `[project.optional-dependencies].kivy` | No `kivy` extra remains in `pyproject.toml`. |
| `tests/test_kivy_optional.py` | Replaced by `tests/test_kivy_removed.py`. |
| Kivy lock entries | `uv lock` dropped `kivy`, `kivymd`, `asynckivy`, `materialshapes`, `materialyoucolor`, Pillow, `pycairo`, and the Windows shims. |

`pyproject.toml` now has no `kivy` or `kivymd` requirement anywhere. A default
install (`uv sync` / `pip install -e .`) cannot pull the Kivy dependency chain.

### 19.2 Runtime configuration

`RuntimeConfig.from_namespace` no longer reads a `kivy` attribute; `ui_mode` is
derived solely from `--ui`. `main.py` has no Kivy branch, import, install hint,
or teardown path. The supported adapters are the Textual TUI (`--ui`), the
simple CLI (no mode flag), the API (`--api`), and headless mode.

Attempting `--kivy` fails immediately at argument parsing:

```text
error: unrecognized arguments: --kivy
```

No peer host is started and no data directory is created.

### 19.3 Verification

```
.venv/bin/python -m unittest discover -s tests
  -> Ran 145 tests ... OK
     (140 after Phases 3–4 + 5 Kivy-removal tests)

grep -ri kivy README.md pyproject.toml uv.lock *.py api app
  -> no matches (historical audit text excepted)
```

`tests/test_kivy_removed.py` asserts:

- `kivy_ui.py` and the Kivy screenshot are gone;
- `pyproject.toml` has no `kivy`/`kivymd` requirement or extra;
- `--help` has no `--kivy`, and `--kivy` exits 2;
- `main`, `headless`, `chatroom`, `tornado_server`, `ui`, `api.base`,
  `app.runtime`, and `app.paths` import with `kivy`/`kivymd` blocked;
- no application `.py` file references Kivy.

### 19.4 Acceptance and explicit exceptions

- [x] No Kivy/KivyMD dependency, optional extra, flag, module, or asset remains.
- [x] Core modules import and run with `kivy`/`kivymd` made unimportable.
- [x] README and CLI help describe only Textual TUI, simple CLI, API, and
  headless modes.
- [x] `uv.lock` no longer contains the Kivy dependency chain.
- [ ] No GUI or mobile UI exists. Android/iOS packaging would require a new UI
  and a separate decision; removing Kivy alone does not produce a mobile build.
- [ ] There is no CI workflow, so the removal test runs locally only.

## 20. Phase 6 — UI audit and adapter integration (implemented)

**Status:** implemented and verified, 2026-11-08.
**Baseline:** `e0653f9` (Phase 5 Kivy removal).
**Scope:** every remaining interface uses the shared runtime and event model;
no adapter owns networking logic or creates a second host.

### 20.1 Interface inventory

| Interface | Entry point | Process / loop | Host owner | Events and status |
|---|---|---|---|---|
| Textual TUI | `python main.py --ui` → `ui.ChatUI` | Main thread (Textual/asyncio); runtime on a daemon trio thread | `ApplicationRuntime` | Subscribes to `EventBus` (`message_received`, `system`, `peer_connected`, `peer_disconnected`); reads `runtime.peer` for status |
| Simple CLI | `python main.py` → `main_async` | One trio loop | `ApplicationRuntime` | Subscribes to `EventBus` for messages/system events; reads `runtime.peer` |
| HTTP API | `python main.py --api` → `TornadoServer` | Tornado/asyncio main thread; runtime on a daemon trio thread | `ApplicationRuntime` | Handlers call runtime services; WebSockets subscribe to `EventBus` |
| React frontend | `py-peer-frontend` (Vite) | Separate browser process | Backend `ApplicationRuntime` | REST + WebSockets; reconnects automatically after backend loss |
| Headless | `python main.py --headless` | One trio loop | `ApplicationRuntime` | Logs plus `GET /api/v1/service/status` when the API is also enabled |
| Route listing | `python main.py --api-routes` | Main thread, exits | None | Static route table only |

No interface calls `new_host` or constructs a `HeadlessService`; the only host
construction site remains `headless.py:_run_service`.

### 20.2 Textual and CLI event integration

Both adapters now consume the runtime `EventBus` instead of the legacy janus
queues:

- `ChatUI` subscribes once with a type filter (`MESSAGE_RECEIVED`, `SYSTEM`,
  `PEER_CONNECTED`, `PEER_DISCONNECTED`), drains the bounded queue on a 0.1 s
  interval, and unsubscribes in `on_unmount`.
- `monitor_runtime_events` does the same for the simple CLI, with a
  `finally` that unsubscribes when the runtime stops.
- The legacy queues remain available for embedders, but adapters consume
  exactly one source; the TUI's fallback only runs when no event bus exists,
  so messages are not duplicated.
- `ApplicationError` is translated into a UI/CLI message rather than a crash.

### 20.3 Runtime modes and host ownership

The CLI modes are mutually exclusive (`--api`, `--ui`, `--headless`, or the
default simple CLI). Each mode calls `build_runtime(args)` exactly once and
either awaits `runtime.start()` or calls `runtime.start_in_thread()`.
Shutdown always goes through `runtime.request_stop()`, `join()`, and `close()`;
the API adapter also stops the IOLoop when the runtime stops.

The Textual import is lazy (inside the `--ui` branch), so headless, CLI, and
API modes import and run without a TUI framework. A subprocess test blocks
`textual` at import time and imports `main`, `headless`, `chatroom`,
`tornado_server`, `api.base`, `app.runtime`, and `app.paths`.

### 20.4 API trust boundary

The API has no authentication and wildcard CORS, so it now binds to
`127.0.0.1` by default (`--api-host`). Binding any other address logs a
warning; exposing the API safely requires an authenticating reverse proxy and
an origin policy. This is documented in `README.md` and `API_REFERENCE.md`.

### 20.5 React recovery

The frontend retries the initial REST load every 3 s instead of stopping on the
first failure, reconnects the message/peer WebSockets after 3 s, and switches
back to the boot screen with a retry message when polling loses the backend.
A successful poll clears the error and restores the chat without a reload.

### 20.6 Verification

```
.venv/bin/python -m unittest discover -s tests
  -> Ran 154 tests ... OK
     (145 after Phase 5 + 9 in tests/test_ui_adapters.py and
      tests/test_api_security.py)

.venv/bin/python -m py_compile main.py ui.py tornado_server.py api/*.py app/*.py
  -> COMPILE_OK

cd py-peer-frontend && npm run build
  -> tsc + vite build OK
cd py-peer-frontend && npm run lint
  -> 0 errors (1 pre-existing react-refresh warning)

python main.py --api --nick smoke6 --port 0 --api-port <ephemeral>
  GET  /api/v1/node/status -> ready, loopback listener
  POST /api/v1/messages    -> 202 queued
  POST /api/v1/service/stop -> clean exit 0
```

### 20.7 Acceptance criteria (brief §10.3)

- [x] Every UI and entry point is documented (§20.1, README).
- [x] Every UI calls the same application services or the defined API.
- [x] No UI contains duplicated networking/business logic.
- [x] Only the runtime owner creates the peer host.
- [x] Headless mode works without UI framework imports.
- [x] React recovers from backend disconnection (REST retry + WS reconnect).
- [x] UI subscription teardown and API shutdown behavior are tested
  (`tests/test_ui_adapters.py`, existing lifecycle tests, API smoke run).
- [x] API security, CORS, and origin behavior are documented.
- [x] The service contracts are framework-independent and ready for a future
  MCP adapter (MCP itself is out of scope).
- [ ] No automated browser test drives the React UI; recovery is implemented
  and builds/lints, but is not exercised end-to-end in CI.
- [ ] No automated test renders the Textual TUI; tests cover its event
  subscription, dispatch, and teardown.
- [ ] The API remains unauthenticated when explicitly exposed; authentication
  is a future adapter/security phase.

## 21. Phase 7 — Docker and Docker Compose (implemented)

**Status:** implemented and verified, 2026-11-09.
**Baseline:** `dd3ddd5` (Phase 6).
**Scope:** container deployment of the headless runtime + HTTP/WebSocket API.
The React frontend is deliberately not bundled.

### 21.1 Files

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build; locked runtime environment; unprivileged runtime image. |
| `.dockerignore` | Keeps `.git`, `.venv`, caches, secrets, local data, tests, and frontend artifacts out of the build context. |
| `compose.yaml` | One `py-peer` service, named `/data` volume, health check, restart policy, log rotation, published ports. |
| `healthcheck.py` | Standard-library readiness probe for `/api/v1/service/status`. |
| `tests/test_docker_assets.py` | Static packaging checks plus real health-probe behaviour tests. |

### 21.2 Image design

- Builder stage: `python:3.12-slim-bookworm` + pinned `uv` copied from
  `ghcr.io/astral-sh/uv:0.13.5`. It installs `git`, `build-essential`, and
  `libgmp-dev`, then runs `uv sync --frozen --no-dev --no-install-project`.
  `uv.lock` is the source of truth, including the libp2p fork commit.
- Runtime stage: `python:3.12-slim-bookworm` with only `ca-certificates` and
  `libgmp10` (needed by `fastecdsa`). The builder's `.venv` and the application
  source are copied in; build tools and `git` do not reach the final image.
- The image runs as `uc` (UID/GID `10001`), not root.
- `UC_DATA_DIR=/data` and `VOLUME ["/data"]` keep identity, SQLite history, and
  the system log out of `/app`.
- `ENTRYPOINT ["python", "main.py"]`; the default command is
  `--api --api-host 0.0.0.0 --nick docker-peer`. This is the headless runtime
  plus the API, which is what the health check needs.
- Only TCP is exposed: `4001/tcp` (libp2p) and `8765/tcp` (API). QUIC,
  WebTransport, and UDP are not configured, so no UDP mapping exists.
- No secrets, `.env`, local database, or frontend build output are copied.

### 21.3 Compose design

- `restart: unless-stopped`, `init: true` (signal forwarding and zombie reaping).
- Named volume `uc-data` mounted at `/data`.
- API port published on host loopback by default
  (`UC_API_PUBLISH_HOST=127.0.0.1`); the container listener binds `0.0.0.0`
  because Docker port forwarding requires it.
- Peer TCP port published on all host interfaces; the host firewall remains the
  operator's control.
- `UC_DATA_DIR`, `UC_HEALTHCHECK_URL`, `UC_NICKNAME`, `UC_PEER_PORT`,
  `UC_API_PORT`, `UC_API_PUBLISH_HOST`, and `UC_ANNOUNCE_HOST` are configurable.
- Health check runs `python /app/healthcheck.py` every 30 s after a 30 s start
  period, with three retries.
- Docker `json-file` logs rotate at 10 MB with three files.

### 21.4 Readiness health check

The API can answer HTTP before the libp2p host is ready, so a 200 response is
not sufficient. `healthcheck.py` parses the JSON body and returns success only
when `data.ready` and `data.running` are true and `data.failed` is false.
`tests/test_docker_assets.py` verifies healthy, not-ready, not-running, failed,
malformed, HTTP-error, and connection-failure cases.

### 21.5 Networking and advertised addresses

Inside the container the listener is `/ip4/0.0.0.0/tcp/4001`. The application's
`AddrsFactory` rewrites that wildcard using `--announce-host`, so the advertised
multiaddr is `/ip4/<UC_ANNOUNCE_HOST>/tcp/4001/p2p/<peer-id>`. Container IPs
such as `172.17.0.x` are never advertised by default.

Verified with `--announce-host 192.0.2.10`:

```text
listen_addrs  ["/ip4/0.0.0.0/tcp/4001"]
multiaddrs    ["/ip4/192.0.2.10/tcp/4001/p2p/12D3KooW..."]
```

For bridge networking, `UC_ANNOUNCE_HOST` must be a host address that peers can
actually dial, and the host must publish TCP 4001. For internet peers the router
must also forward that port. No UPnP, AutoNAT, relay, or public-IP discovery is
implemented. With the default `127.0.0.1`, the address is same-host only.

### 21.6 Verification

```text
docker compose config
  -> valid; command/env/ports/volume/healthcheck rendered

docker build -t py-peer:local .
  -> success (linux/arm64, Docker Desktop)

docker run ... py-peer:local --api --api-host 0.0.0.0 ...
docker exec <container> id
  -> uid=10001(uc) gid=10001(uc)

docker inspect --format '{{.State.Health.Status}}'
  -> healthy
GET /api/v1/service/status
  -> state=ready, ready=true, running=true, failed=false

POST /api/v1/messages {"message":"phase7 persistence check"}
GET  /api/v1/messages/universal-connectivity
  -> total=1 after container restart and after docker rm + re-run

docker stop -t 15 <container>
  -> "Received signal 15; requesting graceful shutdown..."
  -> exit code 0

docker compose down          # volume retained
docker compose up -d
  -> same peer ID and history

docker compose stop
  -> exit code 0

.venv/bin/python -m unittest discover -s tests
  -> 164 tests, OK (154 after Phase 6 + 10 container tests)
```

### 21.7 Acceptance criteria (brief §11)

- [x] The image builds from the repository context without local `.venv`,
  caches, secrets, or frontend artifacts.
- [x] The container starts the headless runtime with the API adapter.
- [x] The container runs as non-root (`uid=10001`).
- [x] The health check reflects actual runtime readiness, not just HTTP 200.
- [x] Durable state is stored on the mounted `/data` volume.
- [x] Peer identity and message history survive container restart and
  recreation (`docker rm` + run, and `compose down` + `up`).
- [x] SIGTERM produces a graceful shutdown and exit code 0.
- [x] Only required TCP ports are exposed/published (`4001`, `8765`).
- [x] Advertised multiaddrs use the operator-supplied host address, not the
  container IP.
- [x] Build, run, logs, `ps`, `down`, persistence, and address caveats are
  documented in `README.md`.

### 21.8 Explicit exceptions

- [ ] NAT/public-internet reachability is not verified. No relay, AutoNAT, UPnP,
  or public-IP discovery exists; a router port forward and a reachable
  `UC_ANNOUNCE_HOST` are required.
- [ ] Cross-machine LAN testing was not possible; the address rewrite was
  verified, but no second physical host dialed the container.
- [ ] Only `linux/arm64` on Docker Desktop was built and run. Multi-arch and
  rootless/CI Docker were not tested; there is no CI workflow.
- [ ] The API remains unauthenticated and wildcard-CORS. Publishing it on a
  non-loopback interface requires an authenticating reverse proxy and firewall.
- [ ] The build requires network access for the base images, PyPI, and the
  libp2p git fork. `uv.lock` pins the resolved versions and fork commit, but it
  is not an offline build.
- [ ] The React frontend is not served by this image; it remains a separate
  deployment.
