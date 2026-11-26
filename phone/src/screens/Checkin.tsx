import { useRef, useState } from "react";
import { ApiError, api, del, errorMessage, getToken, post } from "../api/client";
import { garmentToInbox } from "../api/capture";
import type { Checkin, CheckinGarment, Item, ItemList, PhotoStatus } from "../api/types";
import { tagSummary } from "../components/TagEditor";
import { Empty, Img, LoadError, Loading, Nav, Sheet, Spinner, label } from "../components/ui";
import { toast, useLoad, useWsEvent } from "../lib/hooks";
import { navigate } from "../lib/router";

function PickItem({ g, onClose, onPick }: { g: CheckinGarment; onClose: () => void; onPick: (i: Item) => void }) {
  const cat = g.tags?.category;
  const list = useLoad(() => api<ItemList>(`/api/items?limit=200${cat ? `&category=${encodeURIComponent(cat)}` : ""}`), [cat]);
  return (
    <Sheet title="Which item is it?" onClose={onClose}>
      <div className="list">
        {!list.data ? (
          <Loading />
        ) : list.data.items.length === 0 ? (
          <div className="list-row muted small">No matching items in your closet.</div>
        ) : (
          list.data.items.map((i) => (
            <button key={i.id} className="list-row" onClick={() => onPick(i)}>
              <Img src={i.cutout_url} alt="" style={{ width: 48, height: 60, borderRadius: 8, flex: "none" }} />
              <div className="grow">
                <div className="ellipsis" style={{ fontWeight: 500 }}>
                  {i.name}
                </div>
                <div className="tiny">{tagSummary(i.tags).join(" · ")}</div>
              </div>
            </button>
          ))
        )}
      </div>
    </Sheet>
  );
}

function Start() {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const upload = async (f: File | undefined) => {
    if (!f) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", f, f.name || "checkin.jpg");
      const res = await fetch("/api/checkins/upload", { method: "POST", body: form, headers: { Authorization: `Bearer ${getToken()}` } });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new ApiError(res.status, body.detail);
      navigate(`/checkin/${(body as PhotoStatus).photo_id}`, { replace: true });
    } catch (e) {
      toast({ text: e instanceof TypeError ? "Can't reach the mirror." : errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Empty
      icon="🧍"
      title="What are you wearing?"
      action={
        <div className="stack" style={{ maxWidth: 300, margin: "0 auto" }}>
          <button className="btn big primary block" onClick={() => navigate("/capture")}>
            Take a mirror photo
          </button>
          <input ref={input} className="file-hidden" type="file" accept="image/*" capture="user" onChange={(e) => void upload(e.target.files?.[0])} />
          <button className="btn big block" disabled={busy} onClick={() => input.current?.click()}>
            {busy ? <Spinner small /> : "Use a phone photo"}
          </button>
        </div>
      }
    >
      A photo of today's outfit lets the mirror log what you wore, so it doesn't suggest the same things tomorrow.
    </Empty>
  );
}

export function CheckinScreen({ photoId }: { photoId?: string }) {
  const c = useLoad(() => (photoId ? api<Checkin>(`/api/checkins/${photoId}`) : Promise.resolve(undefined)), [photoId], ["checkin.ready"]);
  const [picked, setPicked] = useState<Record<string, Item>>({}); // detection -> item chosen by hand
  const [off, setOff] = useState<Set<string>>(new Set()); // detections ticked off ("not wearing")
  const [picking, setPicking] = useState<CheckinGarment | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());

  useWsEvent("pipeline.photo", (m) => {
    const d = m.data as { photo_id?: string; status?: string };
    if (d.photo_id === photoId && (d.status === "done" || d.status === "failed")) void c.reload();
  });

  if (!photoId)
    return (
      <div>
        <Nav title="Check in" backTo="/" />
        <Start />
      </div>
    );

  const data = c.data;
  const processing = data && (data.status === "queued" || data.status === "processing");
  const shownItem = (g: CheckinGarment): Item | undefined => picked[g.detection_id] ?? g.match?.item;
  const itemFor = (g: CheckinGarment): string | null => (off.has(g.detection_id) ? null : (shownItem(g)?.id ?? null));
  const selectedIds = data ? [...new Set(data.garments.map(itemFor).filter((x): x is string => !!x))] : [];

  const confirmWorn = async () => {
    setBusy(true);
    try {
      const r = await post<{ worn_on: string; logged: string[]; already_logged: string[] }>(`/api/checkins/${photoId}/confirm`, { item_ids: selectedIds });
      setDone(r.logged.length || r.already_logged.length ? `Logged ${selectedIds.length} item${selectedIds.length === 1 ? "" : "s"} as worn today.` : "Saved: nothing logged as worn.");
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  };
  const toInbox = async (g: CheckinGarment) => {
    try {
      await garmentToInbox(photoId, g.detection_id);
      setSent((s) => new Set(s).add(g.detection_id));
      toast("Sent to the inbox to add as a new item.");
    } catch (e) {
      toast({
        text:
          e instanceof ApiError && e.status === 409
            ? "This one matched something you own, so it isn't a new item."
            : e instanceof ApiError && (e.status === 404 || e.status === 405)
              ? "Sending to the inbox isn't available on this mirror yet. Add it with a flat photo instead."
              : errorMessage(e),
        kind: "error",
      });
    }
  };

  return (
    <div>
      <Nav title="Did you wear these?" backTo="/" />
      <div className="page has-bottom-bar" style={{ paddingTop: 12 }}>
        {c.error && !data ? (
          <LoadError error={c.error} retry={c.reload} />
        ) : !data ? (
          <Loading />
        ) : done ? (
          <Empty icon="✅" title="Thanks!" action={<button className="btn primary" onClick={() => navigate("/", { replace: true })}>Back to today</button>}>
            {done}
          </Empty>
        ) : processing ? (
          <div className="capture-stage">
            <Spinner />
            <div className="capture-state">Looking at your outfit…</div>
            <p className="muted small" style={{ margin: 0 }}>
              {data.step ? `${label(data.step)}…` : "This takes a few seconds."}
            </p>
          </div>
        ) : data.status === "failed" ? (
          <Empty
            icon="⚠️"
            title="Couldn't read this photo"
            action={
              <button
                className="btn primary"
                onClick={async () => {
                  try {
                    await post(`/api/photos/${photoId}/retry`);
                    await c.reload();
                  } catch (e) {
                    toast({ text: errorMessage(e), kind: "error" });
                  }
                }}
              >
                Try again
              </button>
            }
          >
            {data.error || "Something went wrong while processing."}
          </Empty>
        ) : data.garments.length === 0 ? (
          <Empty icon="🤷" title="No clothes found" action={<button className="btn primary" onClick={() => navigate("/capture", { replace: true })}>Retake</button>}>
            The mirror couldn't make out any garments. Step back a little and try again in good light.
          </Empty>
        ) : (
          <>
            <p className="subtitle">Tap to untick anything you're not wearing. One tap logs the rest.</p>
            <div className="list">
              {data.garments.map((g) => {
                const item = shownItem(g);
                const on = !!itemFor(g);
                return (
                  <div className="garment" key={g.detection_id}>
                    <Img src={item?.cutout_url || g.cutout_url} alt={g.label || "garment"} />
                    <div style={{ minWidth: 0 }}>
                      <div className="ellipsis" style={{ fontWeight: 600 }}>
                        {item ? label(item.name) : label(g.label || g.tags?.subcategory || "Garment")}
                      </div>
                      <div className="tiny">
                        {item ? (g.match && g.match.item.id === item.id && g.match.score != null ? `${Math.round(g.match.score * 100)}% match` : "Chosen by you") : "Not matched to your closet"}
                      </div>
                      <div className="row wrap" style={{ gap: 4, marginTop: 6 }}>
                        <button className="btn small plain" style={{ paddingLeft: 0 }} onClick={() => setPicking(g)}>
                          {item ? "Change" : "Pick item"}
                        </button>
                        {!g.match && !g.inbox_id && !sent.has(g.detection_id) && (
                          <button className="btn small plain" style={{ paddingLeft: 0 }} onClick={() => void toInbox(g)}>
                            New item → inbox
                          </button>
                        )}
                        {(sent.has(g.detection_id) || !!g.inbox_id) && <span className="chip ok">In inbox</span>}
                      </div>
                    </div>
                    <button
                      className={`check ${on ? "on" : ""}`}
                      aria-label={on ? "Wearing this" : "Not wearing this"}
                      aria-pressed={on}
                      disabled={!item}
                      onClick={() =>
                        setOff((x) => {
                          const n = new Set(x);
                          if (n.has(g.detection_id)) n.delete(g.detection_id);
                          else n.add(g.detection_id);
                          return n;
                        })
                      }
                    >
                      ✓
                    </button>
                  </div>
                );
              })}
            </div>
            {data.photo_url && (
              <>
                <div className="section-title">Photo</div>
                <Img src={data.photo_url} alt="The check-in photo" cover style={{ aspectRatio: "3 / 4", borderRadius: 14 }} />
              </>
            )}
            <button
              className="btn block danger mt"
              onClick={async () => {
                if (!window.confirm("Delete this photo and everything made from it? This can't be undone.")) return;
                try {
                  await del(`/api/photos/${photoId}`);
                  toast("Photo deleted.");
                  navigate("/", { replace: true });
                } catch (e) {
                  toast({ text: errorMessage(e), kind: "error" });
                }
              }}
            >
              Delete this photo
            </button>
          </>
        )}
      </div>
      {data && !processing && !done && data.status !== "failed" && data.garments.length > 0 && (
        <div className="bottom-bar">
          <button className="btn big primary" disabled={busy} onClick={() => void confirmWorn()}>
            {busy ? <Spinner small /> : selectedIds.length ? `Yes, I wore ${selectedIds.length === 1 ? "this" : `these ${selectedIds.length}`}` : "None of these"}
          </button>
        </div>
      )}
      {picking && (
        <PickItem
          g={picking}
          onClose={() => setPicking(null)}
          onPick={(i) => {
            setPicked((p) => ({ ...p, [picking.detection_id]: i }));
            setOff((x) => {
              const n = new Set(x);
              n.delete(picking.detection_id);
              return n;
            });
            setPicking(null);
          }}
        />
      )}
    </div>
  );
}
