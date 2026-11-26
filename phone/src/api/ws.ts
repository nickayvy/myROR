// WebSocket to /ws. The first message is {"type":"auth","token":...}; the server answers
// "hello". Reconnects with exponential backoff (1 s .. 30 s, jittered), immediately when the
// page becomes visible or the network comes back, and emits "reconnected" so screens refetch.
import { getToken } from "./client";
import type { WsMessage } from "./types";

export type WsStatus = "connecting" | "open" | "closed";
type Handler = (msg: WsMessage) => void;

const handlers = new Map<string, Set<Handler>>();
const statusListeners = new Set<(s: WsStatus) => void>();
let socket: WebSocket | null = null;
let status: WsStatus = "closed";
let attempt = 0;
let retryTimer: ReturnType<typeof setTimeout> | undefined;
let pingTimer: ReturnType<typeof setInterval> | undefined;
let started = false;
let everOpened = false;

function setStatus(s: WsStatus) {
  status = s;
  statusListeners.forEach((l) => l(s));
}
export function wsStatus(): WsStatus {
  return status;
}
export function onWsStatus(l: (s: WsStatus) => void): () => void {
  statusListeners.add(l);
  return () => statusListeners.delete(l);
}

/** Subscribe to one event type ("*" for all). Returns an unsubscribe function. */
export function onWs(type: string, h: Handler): () => void {
  let set = handlers.get(type);
  if (!set) handlers.set(type, (set = new Set()));
  set.add(h);
  return () => set!.delete(h);
}

/** Deliver a message to subscribers as if it came from the server (used by the capture mock). */
export function emitLocal(msg: WsMessage): void {
  handlers.get(msg.type)?.forEach((h) => h(msg));
  handlers.get("*")?.forEach((h) => h(msg));
}

function url(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function connect() {
  clearTimeout(retryTimer);
  const token = getToken();
  if (!token) {
    setStatus("closed");
    return;
  }
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  setStatus("connecting");
  let ws: WebSocket;
  try {
    ws = new WebSocket(url());
  } catch {
    schedule();
    return;
  }
  socket = ws;
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
  ws.onmessage = (ev) => {
    let msg: WsMessage;
    try {
      msg = JSON.parse(ev.data as string);
    } catch {
      return;
    }
    if (msg.type === "hello") {
      attempt = 0;
      setStatus("open");
      clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
      }, 25000);
      // Anything may have changed while we were away: tell screens to refetch.
      emitLocal({ type: everOpened ? "reconnected" : "connected" });
      everOpened = true;
      return;
    }
    emitLocal(msg);
  };
  ws.onclose = (ev) => {
    clearInterval(pingTimer);
    if (socket === ws) socket = null;
    setStatus("closed");
    if (ev.code === 4401) {
      // Token rejected (revoked). The next HTTP call will 401 and clear it.
      attempt = Math.max(attempt, 4);
    }
    schedule();
  };
  ws.onerror = () => {
    /* onclose follows */
  };
}

function schedule() {
  clearTimeout(retryTimer);
  if (!started) return;
  const base = Math.min(30000, 1000 * 2 ** attempt);
  attempt = Math.min(attempt + 1, 6);
  retryTimer = setTimeout(connect, base / 2 + Math.random() * (base / 2));
}

function wake() {
  if (!started) return;
  if (document.visibilityState === "visible" && status !== "open") {
    attempt = 0;
    socket?.close();
    socket = null;
    connect();
  }
}

export function startWs(): void {
  if (started) {
    connect();
    return;
  }
  started = true;
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("online", wake);
  window.addEventListener("pageshow", wake);
  connect();
}

export function stopWs(): void {
  started = false;
  clearTimeout(retryTimer);
  clearInterval(pingTimer);
  socket?.close();
  socket = null;
  setStatus("closed");
}
