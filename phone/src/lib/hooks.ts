import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { onWs, onWsStatus, wsStatus, type WsStatus } from "../api/ws";
import type { WsMessage } from "../api/types";

export interface Loaded<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (fn: (d: T | undefined) => T | undefined) => void;
}

/**
 * Fetch on mount, keep the last good data on error, and refetch when the WebSocket
 * reconnects or when any of `events` arrives.
 */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], events: string[] = []): Loaded<T> {
  const [data, setDataState] = useState<T>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const seq = useRef(0);

  const reload = useCallback(async () => {
    const n = ++seq.current;
    setLoading(true);
    try {
      const d = await fnRef.current();
      if (n === seq.current) {
        setDataState(d);
        setError(undefined);
      }
    } catch (e) {
      if (n === seq.current) setError(e);
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const evKey = events.join(",");
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | undefined;
    const debounced = () => {
      clearTimeout(t);
      t = setTimeout(() => void reload(), 250);
    };
    const offs = ["reconnected", ...evKey.split(",").filter(Boolean)].map((e) => onWs(e, debounced));
    return () => {
      clearTimeout(t);
      offs.forEach((o) => o());
    };
  }, [evKey, reload]);

  const setData = useCallback((f: (d: T | undefined) => T | undefined) => setDataState((d) => f(d)), []);
  return { data, error, loading, reload, setData };
}

export function useWsEvent(type: string, h: (m: WsMessage) => void): void {
  const ref = useRef(h);
  ref.current = h;
  useEffect(() => onWs(type, (m) => ref.current(m)), [type]);
}

export function useWsStatus(): WsStatus {
  return useSyncExternalStore(onWsStatus, wsStatus);
}

export function useOnline(): boolean {
  return useSyncExternalStore(
    (cb) => {
      window.addEventListener("online", cb);
      window.addEventListener("offline", cb);
      return () => {
        window.removeEventListener("online", cb);
        window.removeEventListener("offline", cb);
      };
    },
    () => navigator.onLine,
  );
}

/** A transient message at the bottom of the screen. */
const toastSubs = new Set<(t: Toast | null) => void>();
export interface Toast {
  text: string;
  kind?: "ok" | "error";
  action?: { label: string; run: () => void };
}
let toastTimer: ReturnType<typeof setTimeout> | undefined;
export function toast(t: Toast | string): void {
  const v = typeof t === "string" ? { text: t } : t;
  toastSubs.forEach((s) => s(v));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastSubs.forEach((s) => s(null)), v.action ? 6000 : 3200);
}
export function useToast(): Toast | null {
  const [t, setT] = useState<Toast | null>(null);
  useEffect(() => {
    toastSubs.add(setT);
    return () => {
      toastSubs.delete(setT);
    };
  }, []);
  return t;
}
