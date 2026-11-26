// HTTP client. The device token lives in localStorage (plain HTTP on the LAN: no service
// worker, no cookies needed). A 401 clears the token and sends the user to Pair.

const TOKEN_KEY = "myror.token";
const listeners = new Set<() => void>();

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: token lives for this page only */
  }
  listeners.forEach((l) => l());
}
export function onTokenChange(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** The backend could not be reached at all (Wi-Fi, Pi off, server restarting). */
export class NetworkError extends Error {
  constructor() {
    super("Can't reach the mirror. Check that the phone is on home Wi-Fi.");
    this.name = "NetworkError";
  }
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(describeDetail(status, detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
  /** Validation messages from the server, one per problem, when it sent them. */
  get errors(): string[] {
    const d = this.detail as { errors?: unknown } | undefined;
    if (d && Array.isArray(d.errors)) return d.errors.map(String);
    if (Array.isArray(this.detail))
      return (this.detail as { msg?: string; loc?: unknown[] }[]).map((e) =>
        e.msg ? `${(e.loc || []).slice(1).join(".")} ${e.msg}`.trim() : String(e),
      );
    return [];
  }
}

function describeDetail(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { message?: string; errors?: string[] };
    if (d.message) return d.message;
    if (Array.isArray(d.errors) && d.errors.length) return d.errors.join("; ");
  }
  if (status === 503) return "The mirror's database is unavailable right now. It will retry shortly.";
  if (status >= 500) return "The mirror had a problem. Try again in a moment.";
  return `Request failed (${status}).`;
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

type Opts = { method?: string; body?: unknown; signal?: AbortSignal; timeoutMs?: number };

export async function api<T>(path: string, opts: Opts = {}, retried = false): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs ?? 20000);
  opts.signal?.addEventListener("abort", () => ctrl.abort());
  let res: Response;
  try {
    res = await fetch(path, { method: opts.method || "GET", headers, body, signal: ctrl.signal });
  } catch (e) {
    if (opts.signal?.aborted) throw e;
    throw new NetworkError();
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401 && token) {
    setToken(null);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = text;
  }
  if (res.status === 503 && !retried && (opts.method || "GET") === "GET") {
    // Database briefly unavailable (Neon waking up, etc.): the server says when to retry.
    // Never treat this as an auth problem.
    const after = Math.min(10, Number(res.headers.get("Retry-After")) || 2);
    await new Promise((r) => setTimeout(r, after * 1000));
    return api<T>(path, opts, true);
  }
  if (!res.ok) {
    // Proxies answer 502/504 when the API process is down: treat as unreachable.
    if (res.status === 502 || res.status === 504) throw new NetworkError();
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body });
export const put = <T>(path: string, body?: unknown) => api<T>(path, { method: "PUT", body });
export const patch = <T>(path: string, body?: unknown) => api<T>(path, { method: "PATCH", body });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });
