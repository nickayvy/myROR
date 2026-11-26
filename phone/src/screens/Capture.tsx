import { useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "../api/client";
import { captureAction, currentCapture, startCapture } from "../api/capture";
import type { CaptureState, CaptureStatus } from "../api/types";
import { Banner, Img, Nav, Spinner } from "../components/ui";
import { toast, useWsEvent } from "../lib/hooks";
import { navigate } from "../lib/router";

const ORDER: CaptureState[] = ["cover_wait", "preview", "countdown", "flash", "quality_check", "review"];

const TEXT: Record<CaptureState, string> = {
  idle: "Ready",
  cover_wait: "Slide the camera cover open",
  preview: "Stand in the outline on the mirror",
  countdown: "Hold still",
  flash: "Smile",
  quality_check: "Checking the photo…",
  review: "Keep this photo?",
  cover_reminder: "Close the camera cover",
};

export function Capture() {
  const [cap, setCap] = useState<CaptureStatus | null>(null);
  const [coverOpen, setCoverOpen] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kept, setKept] = useState(false);
  const capRef = useRef<CaptureStatus | null>(null);
  capRef.current = cap;

  async function sync() {
    try {
      setCap(await currentCapture());
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setError("The mirror's camera isn't set up yet (capture routes are missing on the server).");
      else setError(errorMessage(e));
    }
  }
  useEffect(() => {
    void sync();
  }, []);
  useWsEvent("reconnected", () => void sync());
  useWsEvent("capture.state", (m) => {
    const d = m.data as CaptureStatus;
    setCap(d);
    if (d.photo_id && kept) navigate(`/checkin/${d.photo_id}`, { replace: true });
  });
  useWsEvent("cover", (m) => setCoverOpen(!!(m.data as { open?: boolean })?.open));
  useWsEvent("checkin.ready", (m) => {
    const id = (m.data as { photo_id?: string })?.photo_id;
    if (id && kept) navigate(`/checkin/${id}`, { replace: true });
  });

  async function start() {
    setBusy(true);
    setError(null);
    setKept(false);
    try {
      const r = await startCapture();
      setCap((c) => (c && c.capture_id === r.capture_id && c.state !== "idle" ? c : r));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast("A photo is already in progress on the mirror.");
        await sync();
      } else setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function act(action: "keep" | "retake" | "cancel") {
    const id = capRef.current?.capture_id;
    if (!id) return;
    setBusy(true);
    try {
      const r = await captureAction(id, action);
      if (action === "keep") {
        setKept(true);
        if (r?.photo_id) navigate(`/checkin/${r.photo_id}`, { replace: true });
        else toast("Photo kept. You'll be asked what you're wearing in a moment.");
      }
      if (r && r.state) setCap(r);
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
      await sync();
    } finally {
      setBusy(false);
    }
  }

  const state: CaptureState = cap?.state ?? "idle";
  const step = ORDER.indexOf(state);
  const running = state !== "idle" && state !== "cover_reminder";

  return (
    <div>
      <Nav title="Mirror photo" backTo="/" />
      <div className="page" style={{ paddingTop: 8 }}>
        {error && <Banner kind="bad">{error}</Banner>}
        {(state === "cover_reminder" || (coverOpen && !running)) && (
          <Banner kind="warn">
            <b>Camera uncovered.</b> Slide the cover closed on the mirror when you're done.
          </Banner>
        )}

        {!cap ? (
          <div className="capture-stage">
            <Spinner />
          </div>
        ) : state === "idle" || state === "cover_reminder" ? (
          <div className="capture-stage">
            <div style={{ fontSize: 64 }} aria-hidden>
              🪞
            </div>
            <div className="capture-state">{kept ? "Photo kept" : "Check in with the mirror"}</div>
            {!kept && cap.message && <div className="chip">{cap.message}</div>}
            <p className="muted" style={{ maxWidth: 320, margin: 0 }}>
              {kept
                ? "The mirror is working out what you're wearing. You'll get a list to confirm."
                : "The mirror takes a waist-up photo and works out what you're wearing, so you don't have to log it by hand."}
            </p>
            {kept && <Spinner />}
          </div>
        ) : state === "review" ? (
          <div className="capture-stage capture-review">
            <Img src={cap.review_url} alt="The photo the mirror just took" cover fallback="Photo preview" />
            <div className="capture-state">{TEXT.review}</div>
            {cap.seconds_left != null && <div className="tiny">Keeps automatically in {cap.seconds_left} s</div>}
          </div>
        ) : (
          <div className="capture-stage" aria-live="polite">
            {state === "countdown" ? (
              <div className="capture-count">{cap.seconds_left ?? "…"}</div>
            ) : state === "flash" ? (
              <div style={{ fontSize: 80 }} aria-hidden>
                ⚡️
              </div>
            ) : state === "quality_check" ? (
              <Spinner />
            ) : (
              <div style={{ fontSize: 64 }} aria-hidden>
                {state === "cover_wait" ? "👆" : "🧍"}
              </div>
            )}
            <div className="capture-state">{TEXT[state]}</div>
            {cap.message && cap.message !== TEXT[state] && <p className="muted" style={{ margin: 0 }}>{cap.message}</p>}
            <div className="steps" aria-hidden>
              {ORDER.map((s, i) => (
                <span key={s} className={i <= step ? "on" : ""} />
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="bottom-bar">
        {state === "review" ? (
          <>
            <button className="btn big" disabled={busy} onClick={() => void act("retake")}>
              Retake
            </button>
            <button className="btn big primary" disabled={busy} onClick={() => void act("keep")}>
              Keep
            </button>
          </>
        ) : running ? (
          <button className="btn big danger" disabled={busy} onClick={() => void act("cancel")}>
            Cancel
          </button>
        ) : (
          <button className="btn big primary" disabled={busy || !cap} onClick={() => void start()}>
            {busy ? <Spinner small /> : kept ? "Take another" : "Take photo"}
          </button>
        )}
      </div>
    </div>
  );
}
