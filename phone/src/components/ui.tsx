import { useEffect, useState, type ReactNode } from "react";
import { ApiError, NetworkError, errorMessage } from "../api/client";
import { back } from "../lib/router";
import { useToast } from "../lib/hooks";

export function label(v: string | null | undefined): string {
  if (!v) return "";
  const s = v.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Image in a fixed-aspect box: no layout shift, graceful when the link is missing or expired. */
export function Img({
  src,
  alt,
  className = "",
  cover = false,
  fallback = "No image",
  style,
}: {
  src: string | null | undefined;
  alt: string;
  className?: string;
  cover?: boolean;
  fallback?: string;
  style?: React.CSSProperties;
}) {
  const [state, setState] = useState<"loading" | "ok" | "err">("loading");
  useEffect(() => setState("loading"), [src]);
  return (
    <div className={`img ${cover ? "cover" : ""} ${className}`} style={style}>
      {src && state !== "err" ? (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          className={state === "ok" ? "loaded" : ""}
          onLoad={() => setState("ok")}
          onError={() => setState("err")}
        />
      ) : null}
      {(!src || state === "err") && <div className="img-fallback">{src ? "Image unavailable" : fallback}</div>}
    </div>
  );
}

export function Nav({ title, left, right, backTo }: { title: string; left?: ReactNode; right?: ReactNode; backTo?: string }) {
  return (
    <div className="nav">
      <div className="left">
        {left ??
          (backTo !== undefined && (
            <button className="btn plain" onClick={() => back(backTo)} aria-label="Back">
              ‹ Back
            </button>
          ))}
      </div>
      <div className="nav-title">{title}</div>
      <div className="right">{right}</div>
    </div>
  );
}

export function Sheet({
  title,
  onClose,
  children,
  right,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  right?: ReactNode;
}) {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);
  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet" role="dialog" aria-label={title}>
        <div className="sheet-head">
          <button className="btn plain" style={{ justifySelf: "start" }} onClick={onClose}>
            Cancel
          </button>
          <div className="t">{title}</div>
          <div style={{ justifySelf: "end" }}>{right}</div>
        </div>
        <div style={{ paddingTop: 16 }}>{children}</div>
      </div>
    </>
  );
}

export function Empty({ icon, title, children, action }: { icon: string; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="icon" aria-hidden>
        {icon}
      </div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Banner({ kind, children, action }: { kind: "warn" | "bad" | "info" | "ok"; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`banner ${kind}`} role={kind === "bad" ? "alert" : "status"}>
      <div className="grow">{children}</div>
      {action}
    </div>
  );
}

export function Spinner({ small }: { small?: boolean }) {
  return <span className={`spinner ${small ? "sm" : ""}`} aria-label="Loading" />;
}

/** Full-screen-ish error for a failed load, with a retry. */
export function LoadError({ error, retry }: { error: unknown; retry: () => void }) {
  const unreachable = error instanceof NetworkError;
  return (
    <Empty
      icon={unreachable ? "📡" : "⚠️"}
      title={unreachable ? "Can't reach the mirror" : "Something went wrong"}
      action={
        <button className="btn primary" onClick={retry}>
          Try again
        </button>
      }
    >
      {unreachable
        ? "Make sure this phone is on home Wi-Fi and the mirror is switched on."
        : error instanceof ApiError && error.status === 503
          ? "The mirror's database or storage isn't available right now."
          : errorMessage(error)}
    </Empty>
  );
}

export function Loading() {
  return (
    <div className="center" style={{ padding: 48 }}>
      <Spinner />
    </div>
  );
}

export function Toaster() {
  const t = useToast();
  if (!t) return null;
  return (
    <div className={`toast ${t.kind === "error" ? "error" : ""}`} role="status">
      <div className="grow">{t.text}</div>
      {t.action && (
        <button className="btn small" onClick={t.action.run}>
          {t.action.label}
        </button>
      )}
    </div>
  );
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

export function timeOf(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Simple line icons, sized by CSS. */
export const Icon = {
  today: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  ),
  closet: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
      <path d="M8 4l-5 3 2 4 2-1v10h10V10l2 1 2-4-5-3c-.5 1.5-2 2.5-4 2.5S8.5 5.5 8 4z" />
    </svg>
  ),
  add: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <rect x="3" y="3" width="18" height="18" rx="5" />
      <path d="M12 8v8M8 12h8" />
    </svg>
  ),
  inbox: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
      <path d="M3 13l3-8h12l3 8v6H3z" />
      <path d="M3 13h5l1 3h6l1-3h5" />
    </svg>
  ),
  more: (
    <svg viewBox="0 0 24 24" fill="currentColor">
      <circle cx="5" cy="12" r="1.8" />
      <circle cx="12" cy="12" r="1.8" />
      <circle cx="19" cy="12" r="1.8" />
    </svg>
  ),
};
