import { useRef, useState } from "react";
import { Banner, Img, Spinner } from "../components/ui";
import { useOnline } from "../lib/hooks";
import { navigate } from "../lib/router";
import { addFiles, clearFinished, isFinished, remove, retry, retryAllFailed, useUploads, type Upload } from "../upload/queue";

const STEP: Record<string, string> = {
  remove_background: "Removing background",
  tag: "Tagging",
  embed: "Comparing",
  match: "Checking for duplicates",
  waiting_for_budget: "Waiting for AI budget",
  retrying: "Retrying",
};

function describe(u: Upload): { text: string; tone: "" | "ok" | "bad" | "warn" } {
  if (u.state === "waiting") return { text: u.error && u.attempts ? u.error : "Waiting", tone: u.error ? "warn" : "" };
  if (u.state === "uploading") return { text: `Uploading ${Math.round(u.progress * 100)}%`, tone: "" };
  if (u.state === "failed") return { text: u.error || "Upload failed", tone: "bad" };
  const p = u.pipeline;
  if (!p) return { text: "Uploaded", tone: "" };
  if (p.status === "done") return { text: u.duplicate ? "Already uploaded · in inbox or closet" : "Ready in inbox", tone: "ok" };
  if (p.status === "failed") return { text: p.error ? `Processing failed: ${p.error}` : "Processing failed", tone: "bad" };
  if (p.step === "waiting_for_budget") return { text: STEP.waiting_for_budget, tone: "warn" };
  return { text: p.step ? `${STEP[p.step] ?? p.step}…` : p.status === "queued" ? "Queued on the mirror" : "Processing…", tone: "" };
}

function Row({ u }: { u: Upload }) {
  const d = describe(u);
  const pct = u.state === "uploading" ? u.progress : u.state === "uploaded" ? 1 : 0;
  const canRetry = (u.state === "failed" && !!u.blob) || u.pipeline?.status === "failed";
  return (
    <div className="upload-row">
      <Img className="thumb cover" src={u.thumb} alt="" fallback="📷" />
      <div className="grow">
        <div className="ellipsis small" style={{ fontWeight: 500 }}>
          {u.name}
        </div>
        <div className={`tiny ${d.tone ? "" : ""}`} style={{ color: d.tone === "bad" ? "var(--bad)" : d.tone === "ok" ? "var(--ok)" : d.tone === "warn" ? "var(--warn)" : undefined }}>
          {d.text}
        </div>
        {u.state !== "failed" && !isFinished(u) && (
          <div className={`progress ${u.state === "uploaded" ? "done" : ""}`}>
            <div style={{ width: `${pct * 100}%` }} />
          </div>
        )}
      </div>
      {canRetry ? (
        <button className="btn small tinted" onClick={() => retry(u.id)}>
          Retry
        </button>
      ) : u.state === "failed" || u.state === "waiting" ? (
        <button className="btn small icon plain" aria-label={`Remove ${u.name}`} onClick={() => remove(u.id)}>
          ✕
        </button>
      ) : u.state === "uploading" || (u.pipeline && u.pipeline.status !== "done") ? (
        <Spinner small />
      ) : (
        <span style={{ color: "var(--ok)", fontSize: 20, width: 44, textAlign: "center" }} aria-label="Done">
          ✓
        </span>
      )}
    </div>
  );
}

export function AddItems() {
  const uploads = useUploads();
  const camera = useRef<HTMLInputElement>(null);
  const library = useRef<HTMLInputElement>(null);
  const [reading, setReading] = useState(false);
  const online = useOnline();

  const onFiles = async (files: FileList | null) => {
    if (!files || !files.length) return;
    setReading(true);
    try {
      await addFiles(files);
    } finally {
      setReading(false);
      if (camera.current) camera.current.value = "";
      if (library.current) library.current.value = "";
    }
  };

  const counts = {
    sending: uploads.filter((u) => u.state === "uploading" || u.state === "waiting").length,
    processing: uploads.filter((u) => u.state === "uploaded" && u.pipeline?.status !== "done" && u.pipeline?.status !== "failed").length,
    ready: uploads.filter(isFinished).length,
    failed: uploads.filter((u) => u.state === "failed" || u.pipeline?.status === "failed").length,
  };
  const ordered = [...uploads].sort((a, b) => rank(a) - rank(b) || a.addedAt - b.addedAt);

  return (
    <div className={`page ${uploads.length ? "has-bottom-bar" : ""}`}>
      <h1 className="large-title">Add clothes</h1>
      <p className="subtitle">One garment per photo, laid flat on a plain light sheet, in good light. Add as many as you like: they upload in the background.</p>

      {!online && <Banner kind="warn">No connection. Photos are saved on this phone and will upload when you're back on Wi-Fi.</Banner>}

      <input ref={camera} className="file-hidden" type="file" accept="image/*" capture="environment" onChange={(e) => void onFiles(e.target.files)} />
      <input ref={library} className="file-hidden" type="file" accept="image/*" multiple onChange={(e) => void onFiles(e.target.files)} />
      <div className="btn-row">
        <button className="btn big primary" onClick={() => camera.current?.click()}>
          📷 Take photo
        </button>
        <button className="btn big tinted" onClick={() => library.current?.click()}>
          🖼 Choose photos
        </button>
      </div>
      {reading && (
        <p className="tiny center mt">
          <Spinner small /> Reading photos…
        </p>
      )}

      {uploads.length > 0 ? (
        <>
          <div className="card summary-bar mt2">
            <div>
              <div className="n">{counts.sending}</div>
              <div className="tiny">uploading</div>
            </div>
            <div>
              <div className="n">{counts.processing}</div>
              <div className="tiny">processing</div>
            </div>
            <div>
              <div className="n" style={{ color: "var(--ok)" }}>
                {counts.ready}
              </div>
              <div className="tiny">ready</div>
            </div>
            {counts.failed > 0 && (
              <div>
                <div className="n" style={{ color: "var(--bad)" }}>
                  {counts.failed}
                </div>
                <div className="tiny">failed</div>
              </div>
            )}
          </div>
          <div className="row-between">
            <div className="section-title">Photos</div>
            <div className="row" style={{ marginTop: 16 }}>
              {counts.failed > 0 && (
                <button className="btn plain small" onClick={retryAllFailed}>
                  Retry failed
                </button>
              )}
              {counts.ready > 0 && (
                <button className="btn plain small" onClick={clearFinished}>
                  Clear done
                </button>
              )}
            </div>
          </div>
          <div className="list">
            {ordered.map((u) => (
              <Row key={u.id} u={u} />
            ))}
          </div>
          <p className="tiny center mt">You can lock the phone or switch apps. Unsent photos resume when you come back.</p>
          <div className="bottom-bar">
            <button className="btn big primary" disabled={counts.ready === 0} onClick={() => navigate("/inbox")}>
              Review {counts.ready || ""} in inbox
            </button>
          </div>
        </>
      ) : (
        <div className="card pad mt2 small muted">
          <b style={{ color: "var(--text)" }}>Tips for an afternoon of onboarding</b>
          <ul style={{ paddingLeft: 18, margin: "8px 0 0" }}>
            <li>Choose 10–30 photos at once from your library; each uploads on its own and retries if the Wi-Fi drops.</li>
            <li>Identical photos are recognised, so adding one twice is harmless.</li>
            <li>Each photo lands in the inbox, where you confirm or fix its tags.</li>
          </ul>
        </div>
      )}
    </div>
  );
}

function rank(u: Upload): number {
  if (u.state === "failed" || u.pipeline?.status === "failed") return 0;
  if (u.state === "uploading") return 1;
  if (u.state === "waiting") return 2;
  if (!isFinished(u)) return 3;
  return 4;
}
