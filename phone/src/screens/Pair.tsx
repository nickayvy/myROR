import { useEffect, useRef, useState } from "react";
import { ApiError, NetworkError, getToken, post, setToken } from "../api/client";
import type { PairResponse } from "../api/types";
import { Banner, Spinner } from "../components/ui";
import { navigate } from "../lib/router";
import { rememberOwnDevice } from "./Settings";

function codeFromUrl(): string {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const query = new URLSearchParams(location.search);
  return (hash.get("code") || query.get("code") || "").toUpperCase();
}

function defaultName(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  return "Phone";
}

export function Pair() {
  const [code, setCode] = useState(codeFromUrl);
  const [name, setName] = useState(defaultName);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const auto = useRef(false);
  const alreadyPaired = !!getToken();

  async function submit(c = code) {
    const clean = c.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    if (clean.length < 4) {
      setError("Enter the code shown under the QR code on the mirror.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await post<PairResponse>("/api/pair", { code: clean, name: name.trim() || "Phone", kind: "phone" });
      rememberOwnDevice(res.device_id);
      setToken(res.token);
      // Drop the code from the address bar and history.
      navigate("/", { replace: true });
    } catch (e) {
      if (e instanceof NetworkError) setError("Can't reach the mirror. Is this phone on home Wi-Fi?");
      else if (e instanceof ApiError && e.status === 400) setError("That code has expired or was already used. Ask the mirror for a new QR code and scan it again.");
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // A second QR scan while this page is open only changes the hash.
  useEffect(() => {
    const onHash = () => {
      const c = codeFromUrl();
      if (c) {
        setCode(c);
        void submit(c);
      }
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (auto.current) return;
    auto.current = true;
    const c = codeFromUrl();
    if (c && !alreadyPaired) void submit(c);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="page" style={{ paddingTop: "calc(var(--safe-t) + 48px)" }}>
      <div className="center" style={{ fontSize: 56 }} aria-hidden>
        🪞
      </div>
      <h1 className="large-title center">Pair with your mirror</h1>
      <p className="subtitle center">
        On the mirror, open the pairing QR code and scan it with the iPhone Camera app. This phone then controls the mirror on your home Wi-Fi.
      </p>

      {alreadyPaired && (
        <Banner kind="info" action={<button className="btn small" onClick={() => navigate("/", { replace: true })}>Open app</button>}>
          This phone is already paired.
        </Banner>
      )}
      {error && <Banner kind="bad">{error}</Banner>}

      <div className="card pad mt">
        <label className="field">
          <span>Pairing code</span>
          <input
            className="input code"
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            placeholder="ABCD2345"
            autoCapitalize="characters"
            autoCorrect="off"
            autoComplete="one-time-code"
            spellCheck={false}
            maxLength={12}
            inputMode="text"
          />
        </label>
        <label className="field">
          <span>Name for this phone</span>
          <input className="input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
        </label>
        <button className="btn primary big block" disabled={busy} onClick={() => void submit()}>
          {busy ? <Spinner small /> : "Pair"}
        </button>
      </div>
      <p className="tiny center mt2">
        Added this app to your Home Screen? It keeps its own storage, so pair once more from inside it by typing the code shown on the mirror.
      </p>
    </div>
  );
}
