import { useRef, useState } from "react";
import { ApiError, api, errorMessage, post } from "../api/client";
import type { Checkin, OutfitView, OutfitsPayload, PhotoStatus, TodayPayload, WeatherSnapshot } from "../api/types";
import { Banner, Empty, Img, LoadError, Spinner, label, timeOf } from "../components/ui";
import { toast, useLoad, useWsEvent } from "../lib/hooks";
import { navigate } from "../lib/router";

const CONDITION: Record<string, [string, string]> = {
  clear: ["☀️", "Clear"],
  partly_cloudy: ["⛅️", "Partly cloudy"],
  cloudy: ["☁️", "Cloudy"],
  fog: ["🌫️", "Fog"],
  drizzle: ["🌦️", "Drizzle"],
  rain: ["🌧️", "Rain"],
  snow: ["🌨️", "Snow"],
  sleet: ["🌨️", "Sleet"],
  thunderstorm: ["⛈️", "Thunderstorms"],
  unknown: ["🌡️", ""],
};

function temp(w: WeatherSnapshot, c: number | null | undefined): string {
  if (c === null || c === undefined) return "–";
  return `${Math.round(w.display_unit === "F" ? (c * 9) / 5 + 32 : c)}°`;
}

function Weather({ w }: { w: WeatherSnapshot | null | undefined }) {
  if (!w)
    return (
      <div className="card pad muted small">
        No weather yet. Set a location on the mirror (WEATHER_LAT / WEATHER_LON) and it will appear here.
      </div>
    );
  const [icon, cond] = CONDITION[w.condition || "unknown"] ?? CONDITION.unknown;
  return (
    <div className="card weather">
      <div style={{ fontSize: 44 }} aria-hidden>
        {icon}
      </div>
      <div className="temp">{temp(w, w.temp_c)}</div>
      <div className="grow small">
        <div style={{ fontWeight: 600 }}>{w.summary || cond}</div>
        <div className="muted">
          H {temp(w, w.high_c)} · L {temp(w, w.low_c)}
          {w.precip_probability != null && ` · ${Math.round(w.precip_probability * 100)}% rain`}
        </div>
      </div>
    </div>
  );
}

function OutfitCard({ o, busy, onFeedback, onWorn, voted }: { o: OutfitView; busy: boolean; voted?: 1 | -1; onFeedback: (v: 1 | -1) => void; onWorn: () => void }) {
  const items = o.items.slice(0, 4);
  return (
    <div className="card outfit">
      {o.collage_url ? (
        <Img className="collage" src={o.collage_url} alt="Outfit collage" />
      ) : (
        <div className={`outfit-items n${items.length}`}>
          {items.map((i) => (
            <Img key={i.item_id} src={i.cutout_url} alt={i.name || i.slot} fallback={label(i.name || i.slot)} />
          ))}
        </div>
      )}
      <div style={{ padding: 14 }}>
        <div style={{ fontSize: 16, lineHeight: 1.4 }}>{o.reasoning}</div>
        <div className="chips mt">
          {o.items.map((i) => (
            <button key={i.item_id} className="chip" onClick={() => navigate(`/closet/${i.item_id}`)}>
              {label(i.name || i.subcategory || i.slot)}
              {i.state === "dirty" && " · dirty"}
            </button>
          ))}
        </div>
        <div className="thumbs mt">
          <button className={`btn ${voted === 1 ? "selected" : ""}`} aria-label="I like this" aria-pressed={voted === 1} disabled={busy} onClick={() => onFeedback(1)}>
            👍
          </button>
          <button className={`btn ${voted === -1 ? "selected" : ""}`} aria-label="Not for me" aria-pressed={voted === -1} disabled={busy} onClick={() => onFeedback(-1)}>
            👎
          </button>
          <button className="btn tinted" style={{ flex: 2 }} disabled={busy} onClick={onWorn}>
            Wearing this
          </button>
        </div>
        {o.source === "rules" && <div className="tiny mt">Picked by rules (no AI suggestion today).</div>}
      </div>
    </div>
  );
}

export function Today() {
  const today = useLoad(() => api<TodayPayload>("/api/today"), [], ["today.updated"]);
  const outfits = useLoad(() => api<OutfitsPayload>("/api/outfits"), [], ["today.updated", "outfits.updated"]);
  // The newest check-in photo that still has unanswered garments, if any.
  const checkins = useLoad(
    async () => {
      const lists = await Promise.all(
        ["mirror_checkin", "phone_fullbody"].map((s) => api<{ photos: PhotoStatus[] }>(`/api/photos?source=${s}&limit=3`).then((r) => r.photos)),
      );
      const latest = lists.flat().filter((p) => p.status === "done").sort((a, b) => b.uploaded_at.localeCompare(a.uploaded_at))[0];
      if (!latest || Date.now() - new Date(latest.uploaded_at).getTime() > 36 * 3600_000) return [];
      const c = await api<Checkin>(`/api/checkins/${latest.photo_id}`);
      return c.garments.some((g) => g.status === "pending") ? [latest] : [];
    },
    [],
    ["checkin.ready", "wear.logged"],
  );
  const [busy, setBusy] = useState(false);
  const [voted, setVoted] = useState<Record<string, 1 | -1>>({});
  const [index, setIndex] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);

  useWsEvent("today.updated", (m) => {
    if (m.data && typeof m.data === "object") today.setData(() => m.data as TodayPayload);
  });
  useWsEvent("checkin.ready", (m) => {
    const id = (m.data as { photo_id?: string })?.photo_id;
    if (id) toast({ text: "Your mirror photo is ready.", action: { label: "Check in", run: () => navigate(`/checkin/${id}`) } });
  });

  async function reroll() {
    setBusy(true);
    try {
      const res = await post<OutfitsPayload>("/api/outfits/reroll", {});
      outfits.setData(() => res);
      setVoted({});
      setIndex(0);
      scroller.current?.scrollTo({ left: 0, behavior: "smooth" });
      if (!res.outfits.length) toast(res.message || "No other outfits right now.");
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  }
  async function feedback(o: OutfitView, v: 1 | -1) {
    setVoted((x) => ({ ...x, [o.id]: v }));
    try {
      await post(`/api/outfits/${o.id}/feedback`, { value: v });
      toast(v === 1 ? "Noted: more like this." : "Noted: less like this.");
    } catch (e) {
      setVoted((x) => {
        const c = { ...x };
        delete c[o.id];
        return c;
      });
      toast({ text: errorMessage(e), kind: "error" });
    }
  }
  async function worn(o: OutfitView) {
    setBusy(true);
    try {
      const r = await post<{ logged: string[] }>(`/api/outfits/${o.id}/worn`, {});
      toast(r.logged.length ? "Logged as worn today." : "Already logged for today.");
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  const t = today.data;
  const dateLabel = new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  const open = (t?.reminders ?? []).filter((r) => !r.completed);
  const pendingCheckin = (checkins.data ?? [])[0];

  return (
    <div className="page">
      <div className="row-between">
        <div>
          <div className="tiny" style={{ textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 600 }}>
            {dateLabel}
          </div>
          <h1 className="large-title" style={{ marginTop: 2 }}>
            Today
          </h1>
        </div>
        <button className="btn tinted" onClick={() => navigate("/capture")}>
          📷 Mirror photo
        </button>
      </div>

      {today.error && !t ? (
        <LoadError
          error={today.error}
          retry={() => {
            void today.reload();
            void outfits.reload();
          }}
        />
      ) : null}
      {t?.spend_mode === "blocked" && (
        <Banner kind="bad">
          <b>Monthly AI budget reached.</b> Outfits come from simple rules and new photos wait until next month or until the cap is raised.
        </Banner>
      )}
      {t?.spend_mode === "degraded" && (
        <Banner kind="warn">
          <b>AI budget nearly used.</b> The mirror is using cheaper suggestions for the rest of the month.
        </Banner>
      )}
      {t?.stale && t.stale.length > 0 && (
        <Banner kind="warn">
          Out of date: {t.stale.map((s) => label(s)).join(", ")}. The mirror will refresh when it can.
        </Banner>
      )}
      {pendingCheckin && (
        <Banner kind="info" action={<button className="btn small primary" onClick={() => navigate(`/checkin/${pendingCheckin.photo_id}`)}>Review</button>}>
          Did you wear these? Confirm your latest mirror photo.
        </Banner>
      )}

      {today.error && !t ? null : (
        <>
      {!t && !today.error ? (
        <div className="skeleton" style={{ height: 92 }} />
      ) : t ? (
        <Weather w={t.weather} />
      ) : null}

      <div className="row-between">
        <div className="section-title">Outfits</div>
        <button className="btn plain small" disabled={busy || !outfits.data} onClick={() => void reroll()} style={{ marginTop: 16 }}>
          {busy ? <Spinner small /> : "↻ Something else"}
        </button>
      </div>
      {outfits.error && !outfits.data ? (
        <div className="card pad">
          <LoadError error={outfits.error} retry={outfits.reload} />
        </div>
      ) : !outfits.data ? (
        <div className="skeleton" style={{ aspectRatio: "4 / 5", width: "calc(100% - 28px)" }} />
      ) : outfits.data.outfits.length === 0 ? (
        <div className="card">
          <Empty
            icon="👕"
            title="No outfits yet"
            action={
              <div className="btn-row" style={{ maxWidth: 320, margin: "0 auto" }}>
                <button className="btn primary" onClick={() => navigate("/add")}>
                  Add clothes
                </button>
                <button className="btn" disabled={busy} onClick={() => void reroll()}>
                  Try again
                </button>
              </div>
            }
          >
            {outfits.data.message || "Add a few tops, bottoms and shoes, confirm them in the inbox, and the mirror will start suggesting outfits."}
            {outfits.data.notes?.length ? <span className="tiny" style={{ display: "block", marginTop: 8 }}>{outfits.data.notes.join(" ")}</span> : null}
          </Empty>
        </div>
      ) : (
        <>
          <div
            className="outfit-scroller"
            ref={scroller}
            onScroll={(e) => {
              const el = e.currentTarget;
              const w = el.firstElementChild ? (el.firstElementChild as HTMLElement).offsetWidth + 12 : el.clientWidth;
              setIndex(Math.round(el.scrollLeft / w));
            }}
          >
            {outfits.data.outfits.map((o) => (
              <OutfitCard key={o.id} o={o} busy={busy} voted={voted[o.id]} onFeedback={(v) => void feedback(o, v)} onWorn={() => void worn(o)} />
            ))}
          </div>
          {outfits.data.outfits.length > 1 && (
            <div className="dots" aria-hidden>
              {outfits.data.outfits.map((o, i) => (
                <span key={o.id} className={i === index ? "on" : ""} />
              ))}
            </div>
          )}
        </>
      )}

      <div className="section-title">Calendar</div>
      <div className="list">
        {!t ? (
          <div className="event muted small">…</div>
        ) : (t.events ?? []).length === 0 ? (
          <div className="event muted small">Nothing on the calendar today.</div>
        ) : (
          (t.events ?? []).map((e) => (
            <div className="event" key={e.uid}>
              <div className="time">{e.all_day ? "All day" : timeOf(e.start)}</div>
              <div className="grow">
                <div style={{ fontWeight: 500 }}>{e.title}</div>
                {e.location && <div className="tiny">{e.location}</div>}
              </div>
              {e.formality != null && e.formality >= 4 && <span className="chip">Dressy</span>}
            </div>
          ))
        )}
      </div>

      <div className="section-title">Reminders</div>
      <div className="list">
        {!t ? (
          <div className="event muted small">…</div>
        ) : open.length === 0 ? (
          <div className="event muted small">
            {t.last_sync?.reminders ? "All done." : "No reminders synced yet. Set up the Reminders Shortcut in More › Settings."}
          </div>
        ) : (
          open.map((r) => (
            <div className="event" key={r.id}>
              <div className="time">{r.due ? timeOf(r.due) : ""}</div>
              <div className="grow">
                <div>{r.title}</div>
                {r.list && <div className="tiny">{r.list}</div>}
              </div>
            </div>
          ))
        )}
      </div>
        </>
      )}
      {today.error && t ? <p className="tiny center mt">Showing the last update. {today.error instanceof ApiError ? "" : "The mirror is unreachable."}</p> : null}
    </div>
  );
}
