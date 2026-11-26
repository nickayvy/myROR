import { useState } from "react";
import { api, del, errorMessage, getToken, post, setToken } from "../api/client";
import type { Device, Health, IntegrationStatus, MintResponse } from "../api/types";
import { Banner, LoadError, Loading, Nav, Spinner, label, relTime } from "../components/ui";
import { toast, useLoad } from "../lib/hooks";

const OWN_DEVICE_KEY = "myror.device_id";

/** Text the user can copy: tries the clipboard (often blocked on plain HTTP) and falls back to selecting it. */
function CopyField({ value }: { value: string }) {
  const rows = Math.max(1, Math.ceil(value.length / 24));
  const [copied, setCopied] = useState(false);
  let ref: HTMLTextAreaElement | null = null;
  const copy = async () => {
    ref?.focus();
    ref?.setSelectionRange(0, value.length);
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Plain HTTP: no clipboard API. The text is selected; iOS shows the Copy menu on a tap.
      toast("Selected. Tap the text and choose Copy.");
    }
  };
  return (
    <div className="row" style={{ alignItems: "stretch" }}>
      <textarea
        ref={(r) => {
          ref = r;
        }}
        className="input mono grow"
        readOnly
        rows={rows}
        value={value}
        onFocus={(e) => e.currentTarget.select()}
        style={{ minHeight: 44, height: 20 + rows * 21, fontSize: 14, lineHeight: "21px", resize: "none" }}
      />
      <button className="btn small tinted" style={{ minHeight: 44 }} onClick={() => void copy()}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function Stat({ k, v, tone }: { k: string; v: React.ReactNode; tone?: "ok" | "bad" | "warn" }) {
  return (
    <div className="list-row" style={{ justifyContent: "space-between" }}>
      <span>{k}</span>
      <span className="small" style={{ color: tone === "bad" ? "var(--bad)" : tone === "warn" ? "var(--warn)" : tone === "ok" ? "var(--ok)" : "var(--text-2)", textAlign: "right" }}>
        {v}
      </span>
    </div>
  );
}

export function Settings() {
  const devices = useLoad(() => api<Device[]>("/api/devices"), []);
  const health = useLoad(() => api<Health>("/api/health"), []);
  const status = useLoad(() => api<IntegrationStatus>("/api/today/status"), [], ["today.updated"]);
  const [minted, setMinted] = useState<MintResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const own = (() => {
    try {
      return localStorage.getItem(OWN_DEVICE_KEY);
    } catch {
      return null;
    }
  })();

  const phones = (devices.data ?? []).filter((d) => d.kind !== "shortcut");
  const shortcuts = (devices.data ?? []).filter((d) => d.kind === "shortcut");
  const origin = location.origin;

  const revoke = async (d: Device) => {
    const self = d.id === own;
    if (!confirm(self ? "Unpair this phone? You'll need to scan the mirror's QR code again." : `Revoke “${d.name}”? It stops working immediately.`)) return;
    try {
      await del(`/api/devices/${d.id}`);
      if (self) {
        setToken(null);
        return;
      }
      devices.setData((x) => x?.filter((y) => y.id !== d.id));
      toast("Revoked.");
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    }
  };
  const mint = async () => {
    setBusy(true);
    try {
      const r = await post<MintResponse>("/api/shortcut/tokens", { name: "Reminders Shortcut" });
      setMinted(r);
      void devices.reload();
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const h = health.data;
  const s = status.data;
  const spend = h?.spend;

  return (
    <div>
      <Nav title="Settings" backTo="/more" />
      <div className="page" style={{ paddingTop: 4 }}>
        <div className="section-title">AI budget</div>
        {health.error && !h ? (
          <LoadError error={health.error} retry={health.reload} />
        ) : !h ? (
          <Loading />
        ) : (
          <div className="card pad">
            {spend ? (
              <>
                <div className="row-between">
                  <div style={{ fontSize: 22, fontWeight: 700 }}>
                    ${spend.spent_usd.toFixed(2)} <span className="muted small" style={{ fontWeight: 400 }}>of ${spend.cap_usd.toFixed(2)}</span>
                  </div>
                  <span className={`chip ${spend.mode === "blocked" ? "bad" : spend.mode === "degraded" ? "warn" : "ok"}`}>
                    {spend.mode === "blocked" ? "Cap reached" : spend.mode === "degraded" ? "Saving" : "Normal"}
                  </span>
                </div>
                <div className="progress" style={{ height: 8, borderRadius: 4, marginTop: 10 }}>
                  <div style={{ width: `${Math.min(100, spend.percent)}%`, background: spend.mode === "blocked" ? "var(--bad)" : spend.mode === "degraded" ? "var(--warn)" : undefined }} />
                </div>
                <div className="tiny mt">
                  {spend.ledger === "dev" ? "Development ledger (one-time cap)." : "This month."} The mirror switches to cheaper suggestions near the cap and stops paid calls at it.
                </div>
              </>
            ) : (
              <div className="muted small">Spend status is unavailable (database offline).</div>
            )}
          </div>
        )}

        <div className="section-title">Sync</div>
        <div className="list">
          {!s ? (
            status.error ? <Stat k="Status" v="Unavailable" tone="bad" /> : <Loading />
          ) : (
            <>
              <Stat
                k="Weather"
                v={!s.weather.configured ? "Not set up" : s.weather.stale ? `Stale · ${relTime(s.weather.last_success)}` : relTime(s.weather.last_success)}
                tone={!s.weather.configured ? undefined : s.weather.stale ? "warn" : "ok"}
              />
              {Object.entries(s.calendars as Record<string, { configured: boolean; stale?: boolean; last_success?: string | null; last_error?: string | null }>).map(([k, c]) => (
                <Stat
                  key={k}
                  k={`${k === "icloud" ? "iCloud" : label(k)} calendar`}
                  v={!c.configured ? "Not set up" : c.last_error ? `Error · ${relTime(c.last_success)}` : c.stale ? `Stale · ${relTime(c.last_success)}` : relTime(c.last_success)}
                  tone={!c.configured ? undefined : c.last_error ? "bad" : c.stale ? "warn" : "ok"}
                />
              ))}
              <Stat k="Reminders" v={s.reminders.last_push ? `${s.reminders.count} · ${relTime(s.reminders.last_push)}` : "Never synced"} tone={s.reminders.last_push ? "ok" : undefined} />
              <Stat k="Outfits" v={s.outfits.last_sync ? `${s.outfits.today} today · ${relTime(s.outfits.last_sync)}` : "None yet"} />
            </>
          )}
        </div>

        <div className="section-title">Reminders Shortcut</div>
        <div className="card pad small">
          <p style={{ marginTop: 0 }}>
            An iPhone Shortcut sends your Reminders to the mirror. It needs its own token. Create one, then follow the steps in the project's <span className="mono">server/integrations/README.md</span>.
          </p>
          {minted ? (
            <>
              <Banner kind="warn">
                <b>Copy the token now.</b> It is shown only once. Anyone with it can send reminders to the mirror.
              </Banner>
              <div className="field">
                <span>URL (Get Contents of URL)</span>
                <CopyField value={`${origin}/api/reminders`} />
              </div>
              <div className="field">
                <span>Method</span>
                <div className="mono">POST · Request Body: File (the dictionary)</div>
              </div>
              <div className="field">
                <span>Header: Authorization</span>
                <CopyField value={`Bearer ${minted.token}`} />
              </div>
              <div className="field">
                <span>Header: Content-Type</span>
                <CopyField value="application/json" />
              </div>
              <button className="btn block" onClick={() => setMinted(null)}>
                Done
              </button>
            </>
          ) : (
            <button className="btn primary block" disabled={busy} onClick={() => void mint()}>
              {busy ? <Spinner small /> : shortcuts.length ? "Create another token" : "Create Shortcut token"}
            </button>
          )}
        </div>
        {shortcuts.length > 0 && (
          <div className="list mt">
            {shortcuts.map((d) => (
              <div className="list-row" key={d.id}>
                <div className="grow">
                  <div>{d.name}</div>
                  <div className="tiny">Last used {relTime(d.last_seen_at)}</div>
                </div>
                <button className="btn small danger" onClick={() => void revoke(d)}>
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="section-title">Paired devices</div>
        <div className="list">
          {devices.error && !devices.data ? (
            <Stat k="Devices" v="Unavailable" tone="bad" />
          ) : !devices.data ? (
            <Loading />
          ) : (
            phones.map((d) => (
              <div className="list-row" key={d.id}>
                <div className="grow">
                  <div>
                    {d.name} {d.id === own && <span className="chip accent">This phone</span>}
                  </div>
                  <div className="tiny">
                    Paired {relTime(d.paired_at)} · seen {relTime(d.last_seen_at)}
                  </div>
                </div>
                <button className="btn small danger" onClick={() => void revoke(d)}>
                  {d.id === own ? "Unpair" : "Revoke"}
                </button>
              </div>
            ))
          )}
        </div>
        <p className="tiny" style={{ margin: "8px 4px 0" }}>
          To pair another phone, show the pairing QR code on the mirror and scan it with that phone's Camera app.
        </p>

        <div className="section-title">Mirror</div>
        <div className="list">
          {h && (
            <>
              <Stat k="Database" v={h.db.ok ? "OK" : h.db.error || "Offline"} tone={h.db.ok ? "ok" : "bad"} />
              <Stat k="Photo storage" v={h.storage.ok ? "OK" : h.storage.error || "Offline"} tone={h.storage.ok ? "ok" : "bad"} />
              <Stat k="AI tagging and outfits" v={h.providers.llm ? "Set up" : "No API key"} tone={h.providers.llm ? "ok" : "warn"} />
              <Stat k="Background removal" v={h.providers.fal ? "Set up" : "No API key"} tone={h.providers.fal ? "ok" : "warn"} />
              <Stat k="Mirror screen" v={h.ws_clients.mirror ? "Connected" : "Not connected"} tone={h.ws_clients.mirror ? "ok" : "warn"} />
            </>
          )}
        </div>
        {!own && getToken() && (
          <button
            className="btn block danger mt2"
            onClick={() => {
              if (confirm("Forget the pairing on this phone? The device stays listed until you revoke it.")) setToken(null);
            }}
          >
            Forget pairing on this phone
          </button>
        )}
      </div>
    </div>
  );
}

export function rememberOwnDevice(id: string): void {
  try {
    localStorage.setItem(OWN_DEVICE_KEY, id);
  } catch {
    /* ignore */
  }
}
