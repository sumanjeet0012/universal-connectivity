import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react'
import * as api from '../api/client'
import type { ChatMessage, NodeInfo, ServiceStatus, TopicInfo } from '../api/client'

// ─── Types ────────────────────────────────────────────────────────────────────

interface PeerPeerState {
  nodeInfo: NodeInfo | null
  status: ServiceStatus | null
  // keyed by topic name
  topics: Record<string, TopicInfo>
  // keyed by topic name
  messages: Record<string, ChatMessage[]>
  connectedPeers: string[]
  loading: boolean
  error: string
  activeTopic: string
  setActiveTopic: (topic: string) => void
  sendMessage: (topic: string, text: string) => Promise<void>
  connectPeer: (maddr: string) => Promise<void>
  subscribeTopic: (topic: string) => Promise<void>
  refreshPeers: () => void
  markRead: (topic: string) => void
}

// ─── Context ──────────────────────────────────────────────────────────────────

const PyPeerContext = createContext<PeerPeerState | null>(null)

export function usePyPeer(): PeerPeerState {
  const ctx = useContext(PyPeerContext)
  if (!ctx) throw new Error('usePyPeer must be used inside <PyPeerProvider>')
  return ctx
}

// ─── Provider ─────────────────────────────────────────────────────────────────

const DEFAULT_TOPIC = 'universal-connectivity'
const POLL_INTERVAL = 5000
const RETRY_INTERVAL = 3000

function sameMessage(a: ChatMessage, b: ChatMessage): boolean {
  return a.timestamp === b.timestamp && a.sender_id === b.sender_id && a.message === b.message
}

export function PyPeerProvider({ children }: { children: React.ReactNode }) {
  const [nodeInfo, setNodeInfo] = useState<NodeInfo | null>(null)
  const [status, setStatus] = useState<ServiceStatus | null>(null)
  const [topics, setTopics] = useState<Record<string, TopicInfo>>({})
  const [messages, setMessages] = useState<Record<string, ChatMessage[]>>({})
  const [connectedPeers, setConnectedPeers] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeTopic, setActiveTopic] = useState(DEFAULT_TOPIC)

  // The WebSocket handler must see the current topic without reconnecting on
  // every topic switch.
  const activeTopicRef = useRef(activeTopic)
  useEffect(() => {
    activeTopicRef.current = activeTopic
  }, [activeTopic])

  // ── Initial load ─────────────────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false
    let retryTimer: ReturnType<typeof setTimeout> | undefined

    async function init() {
      try {
        const [info, svc] = await Promise.all([api.getNodeInfo(), api.getServiceStatus()])
        if (cancelled) return
        setNodeInfo(info)
        setStatus(svc)
        setError('')
        setLoading(false)
      } catch {
        if (cancelled) return
        // Keep the boot screen up and retry automatically, so a backend
        // restart recovers without a manual page reload.
        setError('Cannot reach the py-peer API. Retrying automatically…')
        retryTimer = setTimeout(init, RETRY_INTERVAL)
      }
    }

    init()
    return () => {
      cancelled = true
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [])

  // ── Poll topics + peers ───────────────────────────────────────────────────

  const refreshTopics = useCallback(async () => {
    try {
      const { topics } = await api.getTopics()
      setTopics(topics)
    } catch { /* silent */ }
  }, [])

  const refreshPeers = useCallback(async () => {
    try {
      const { peers } = await api.getPeers()
      setConnectedPeers(peers)
      const svc = await api.getServiceStatus()
      setStatus(svc)
      // The backend is reachable again; clear the boot-screen error so the
      // chat reappears without a page reload.
      setError('')
    } catch {
      setError('Lost connection to the py-peer API. Retrying automatically…')
    }
  }, [])

  useEffect(() => {
    if (loading) return
    refreshTopics()
    refreshPeers()
    const id = setInterval(() => {
      refreshTopics()
      refreshPeers()
    }, POLL_INTERVAL)
    return () => clearInterval(id)
  }, [loading, refreshTopics, refreshPeers])

  // ── WebSocket: real-time message feed ────────────────────────────────────

  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (loading) return

    let disposed = false
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let ws: WebSocket | null = null

    function connect() {
      if (disposed) return
      ws = api.wsMessages()
      wsRef.current = ws

      ws.onmessage = (evt) => {
        try {
          const frame = JSON.parse(evt.data as string) as { event: string; data: ChatMessage }
          const msg = frame.data
          if (!msg?.topic) return

          const isActive = msg.topic === activeTopicRef.current

          setMessages((prev) => {
            const existing = prev[msg.topic] ?? []
            // Replace our optimistic copy with the server echo (timestamps differ,
            // so the plain dedupe below would show the message twice).
            const pendingIdx = existing.findIndex(
              (m) => m.pending && m.sender_id === msg.sender_id && m.message === msg.message,
            )
            if (pendingIdx !== -1) {
              const next = existing.slice()
              next[pendingIdx] = { ...msg, read: isActive || msg.read }
              return { ...prev, [msg.topic]: next }
            }
            if (existing.some((m) => sameMessage(m, msg))) return prev
            return { ...prev, [msg.topic]: [...existing, { ...msg, read: isActive || msg.read }] }
          })

          if (isActive) {
            // The user is looking at this topic, so keep the server's unread
            // state in sync instead of showing a badge for messages already read.
            api.markRead(msg.topic).catch(() => {})
          }

          setTopics((prev) => {
            const t = prev[msg.topic]
            if (!t) return prev
            return {
              ...prev,
              [msg.topic]: {
                ...t,
                unread_count: isActive ? 0 : t.unread_count + 1,
                total_count: t.total_count + 1,
                last_message: msg,
              },
            }
          })
        } catch { /* ignore malformed */ }
      }

      ws.onclose = () => {
        if (disposed) return
        // Reconnect after 3 s
        retryTimer = setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      disposed = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
      wsRef.current = null
    }
  }, [loading])

  // ── WebSocket: peer updates ───────────────────────────────────────────────

  const wsPeersRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (loading) return

    let disposed = false
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let ws: WebSocket | null = null

    function connect() {
      if (disposed) return
      ws = api.wsPeers()
      wsPeersRef.current = ws

      ws.onmessage = (evt) => {
        try {
          const frame = JSON.parse(evt.data as string) as {
            event: string
            data: { connected_peers: string[]; peer_count: number }
          }
          if (frame.event === 'peer_update') {
            setConnectedPeers(frame.data.connected_peers)
          }
        } catch { /* ignore */ }
      }

      ws.onclose = () => {
        if (disposed) return
        retryTimer = setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      disposed = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
      wsPeersRef.current = null
    }
  }, [loading])

  // ── Fetch historical messages for active topic ─────────────────────────

  useEffect(() => {
    if (!activeTopic || loading) return
    let cancelled = false
    const topic = activeTopic

    async function loadHistory() {
      try {
        const first = await api.getMessages(topic)
        let history = first.messages

        // The API pages from the oldest message, so a busy topic would otherwise
        // show only its first page and silently drop everything newer.
        if (first.total > history.length) {
          const offset = Math.max(0, first.total - first.limit)
          const tail = await api.getMessages(topic, first.limit, offset)
          history = tail.messages
        }

        if (cancelled) return
        setMessages((prev) => {
          const existing = prev[topic] ?? []
          const merged = [...history]
          for (const m of existing) {
            if (!merged.some((h) => sameMessage(h, m))) merged.push(m)
          }
          merged.sort((a, b) => a.timestamp - b.timestamp)
          return { ...prev, [topic]: merged }
        })
      } catch { /* topic may not exist yet */ }
    }

    loadHistory()
    return () => { cancelled = true }
  }, [activeTopic, loading])

  // ── Actions ───────────────────────────────────────────────────────────────

  const sendMessage = useCallback(async (topic: string, text: string) => {
    await api.sendMessage(topic, text)
    // Optimistically add own message
    const optimistic: ChatMessage = {
      type: 'chat_message',
      message: text,
      sender_nick: nodeInfo?.nickname ?? 'you',
      sender_id: nodeInfo?.peer_id ?? 'self',
      timestamp: Date.now() / 1000,
      topic,
      read: true,
      pending: true,
    }
    setMessages((prev) => ({ ...prev, [topic]: [...(prev[topic] ?? []), optimistic] }))
  }, [nodeInfo])

  const connectPeer = useCallback(async (maddr: string) => {
    await api.connectToPeer(maddr)
    setTimeout(refreshPeers, 1500)
  }, [refreshPeers])

  const subscribeTopic = useCallback(async (topic: string) => {
    await api.subscribeTopic(topic)
    setTimeout(refreshTopics, 1000)
  }, [refreshTopics])

  const markRead = useCallback((topic: string) => {
    api.markRead(topic).catch(() => {})
    setTopics((prev) => {
      const t = prev[topic]
      if (!t) return prev
      return { ...prev, [topic]: { ...t, unread_count: 0 } }
    })
    setMessages((prev) => {
      const msgs = (prev[topic] ?? []).map((m) => ({ ...m, read: true }))
      return { ...prev, [topic]: msgs }
    })
  }, [])

  return (
    <PyPeerContext.Provider
      value={{
        nodeInfo,
        status,
        topics,
        messages,
        connectedPeers,
        loading,
        error,
        activeTopic,
        setActiveTopic,
        sendMessage,
        connectPeer,
        subscribeTopic,
        markRead,
        refreshPeers,
      }}
    >
      {children}
    </PyPeerContext.Provider>
  )
}
