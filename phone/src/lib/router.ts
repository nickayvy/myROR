// Tiny history router: the backend serves index.html for every non-API path.
import { useSyncExternalStore } from "react";

const subs = new Set<() => void>();
let depth = 0; // in-app pushes we can safely go back through
window.addEventListener("popstate", () => {
  depth = Math.max(0, depth - 1);
  subs.forEach((s) => s());
});

export function navigate(to: string, opts: { replace?: boolean } = {}): void {
  if (to === location.pathname + location.search + location.hash) return;
  if (opts.replace) history.replaceState(null, "", to);
  else {
    history.pushState(null, "", to);
    depth++;
  }
  window.scrollTo(0, 0);
  subs.forEach((s) => s());
}

export function back(fallback = "/"): void {
  if (depth > 0) history.back();
  else navigate(fallback, { replace: true });
}

export function usePath(): string {
  return useSyncExternalStore(
    (cb) => {
      subs.add(cb);
      return () => subs.delete(cb);
    },
    () => location.pathname,
  );
}

/** Match "/closet/:id" style patterns. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split("/").filter(Boolean);
  const s = path.split("/").filter(Boolean);
  if (p.length !== s.length) return null;
  const out: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    if (p[i].startsWith(":")) out[p[i].slice(1)] = decodeURIComponent(s[i]);
    else if (p[i] !== s[i]) return null;
  }
  return out;
}
