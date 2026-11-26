import { useMemo, useState } from "react";
import { ApiError, api, errorMessage, post } from "../api/client";
import type { BulkResult, Health, Inbox, InboxEntry, Item, ItemList, Tags } from "../api/types";
import { cleanTags, missingTags, tagSummary, TagEditor } from "../components/TagEditor";
import { Banner, Empty, Img, LoadError, Loading, Sheet, Spinner, label } from "../components/ui";
import { toast, useLoad } from "../lib/hooks";
import { navigate } from "../lib/router";

type Draft = { tags: Tags; name?: string };

function baseTags(e: InboxEntry): Tags {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { name: _n, ...t } = e.proposed_tags || {};
  return t;
}
function defaultName(t: Tags): string {
  return [t.primary_color, t.subcategory || t.category].filter(Boolean).map((x) => String(x).replace(/_/g, " ")).join(" ");
}
function aiKeyMissing(e: InboxEntry): boolean {
  return e.tag_errors.some((m) => /API_KEY|not set|not configured/i.test(m));
}

function EditSheet({ entry, draft, onClose, onSave, onConfirm, onMerge }: { entry: InboxEntry; draft: Draft; onClose: () => void; onSave: (d: Draft) => void; onConfirm: (d: Draft) => Promise<void>; onMerge: () => void }) {
  const [d, setD] = useState<Draft>(draft);
  const [busy, setBusy] = useState(false);
  const [errs, setErrs] = useState<string[]>([]);
  const missing = missingTags(d.tags);
  return (
    <Sheet
      title="Edit tags"
      onClose={onClose}
      right={
        <button
          className="btn plain"
          onClick={() => {
            onSave(d);
            onClose();
          }}
        >
          Save
        </button>
      }
    >
      <div className="row" style={{ gap: 12, marginBottom: 16 }}>
        <Img src={entry.cutout_url || entry.photo_url} alt="Garment" style={{ width: 84, aspectRatio: "3 / 4", borderRadius: 10, flex: "none" }} />
        <div className="small muted">
          {entry.tagging === "ok" ? "Check the suggested tags and fix anything that's off." : "Some tags are missing. Fill in the starred fields."}
        </div>
      </div>
      <TagEditor tags={d.tags} name={d.name ?? defaultName(d.tags)} onChange={(tags) => setD({ ...d, tags })} onName={(name) => setD({ ...d, name })} />
      {errs.map((e) => (
        <div className="field-error" key={e}>
          {e}
        </div>
      ))}
      <button
        className="btn primary big block mt"
        disabled={busy || missing.length > 0}
        onClick={async () => {
          setBusy(true);
          setErrs([]);
          try {
            await onConfirm(d);
            onClose();
          } catch (e) {
            setErrs(e instanceof ApiError && e.errors.length ? e.errors : [errorMessage(e)]);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? <Spinner small /> : missing.length ? `Still needed: ${missing.join(", ")}` : "Save and add to closet"}
      </button>
      <button className="btn plain block mt" onClick={onMerge}>
        Already own this? Merge with an item
      </button>
    </Sheet>
  );
}

function MergeSheet({ entry, onClose, onMerge }: { entry: InboxEntry; onClose: () => void; onMerge: (item: Item) => Promise<void> }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const cat = entry.proposed_tags?.category;
  const items = useLoad(
    () => api<ItemList>(`/api/items?limit=60${q ? `&q=${encodeURIComponent(q)}` : cat ? `&category=${encodeURIComponent(cat)}` : ""}`),
    [q, cat],
  );
  const suggested = entry.match?.item;
  const list = (items.data?.items ?? []).filter((i) => i.id !== suggested?.id);
  const pick = async (i: Item) => {
    setBusy(i.id);
    try {
      await onMerge(i);
      onClose();
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(null);
    }
  };
  const row = (i: Item, note?: string) => (
    <button key={i.id} className="list-row" disabled={!!busy} onClick={() => void pick(i)}>
      <Img src={i.cutout_url} alt="" style={{ width: 48, height: 60, borderRadius: 8, flex: "none" }} />
      <div className="grow">
        <div className="ellipsis" style={{ fontWeight: 500 }}>
          {i.name}
        </div>
        <div className="tiny">{note || tagSummary(i.tags).join(" · ")}</div>
      </div>
      {busy === i.id ? <Spinner small /> : <span className="chip accent">Merge</span>}
    </button>
  );
  return (
    <Sheet title="Same as an item you own" onClose={onClose}>
      <p className="small muted" style={{ marginTop: 0 }}>
        Adds this photo to an existing item instead of creating a new one.
      </p>
      {suggested && (
        <>
          <div className="section-title" style={{ marginTop: 0 }}>
            Best match
          </div>
          <div className="list">{row(suggested, entry.match?.score != null ? `${Math.round(entry.match.score * 100)}% similar` : undefined)}</div>
        </>
      )}
      <div className="section-title">Your closet</div>
      <input className="input" placeholder="Search by name" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="list mt">
        {items.loading && !items.data ? <Loading /> : list.length === 0 ? <div className="list-row muted small">No items found.</div> : list.map((i) => row(i))}
      </div>
    </Sheet>
  );
}

export function InboxScreen({ onChanged }: { onChanged: () => void }) {
  const inbox = useLoad(() => api<Inbox>("/api/inbox?limit=500"), [], ["inbox.updated", "inbox.changed"]);
  const health = useLoad(() => api<Health>("/api/health"), []);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [editing, setEditing] = useState<InboxEntry | null>(null);
  const [merging, setMerging] = useState<InboxEntry | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  const entries = useMemo(() => inbox.data?.entries ?? [], [inbox.data]);
  const draftOf = (e: InboxEntry): Draft => drafts[e.id] ?? { tags: baseTags(e) };
  const ready = useMemo(() => entries.filter((e) => missingTags((drafts[e.id] ?? { tags: baseTags(e) }).tags).length === 0), [entries, drafts]);

  const drop = (ids: string[]) => {
    inbox.setData((d) => d && { count: d.count - ids.length, entries: d.entries.filter((e) => !ids.includes(e.id)) });
    setSelected((s) => new Set([...s].filter((x) => !ids.includes(x))));
    onChanged();
  };
  const withBusy = async (id: string, fn: () => Promise<void>) => {
    setBusy((b) => new Set(b).add(id));
    try {
      await fn();
    } finally {
      setBusy((b) => {
        const n = new Set(b);
        n.delete(id);
        return n;
      });
    }
  };

  const confirmOne = async (e: InboxEntry, d: Draft = draftOf(e)) => {
    const body: { tags: Tags; name?: string } = { tags: cleanTags(d.tags) };
    if (d.name?.trim()) body.name = d.name.trim();
    const r = await post<{ item: Item }>(`/api/inbox/${e.id}/confirm`, body);
    drop([e.id]);
    toast({ text: `Added “${r.item.name}” to the closet.`, kind: "ok", action: { label: "View", run: () => navigate(`/closet/${r.item.id}`) } });
  };
  const reject = (e: InboxEntry) =>
    withBusy(e.id, async () => {
      try {
        await post(`/api/inbox/${e.id}/reject`);
        drop([e.id]);
        toast("Removed from the inbox.");
      } catch (err) {
        toast({ text: errorMessage(err), kind: "error" });
      }
    });
  const merge = async (e: InboxEntry, item: Item) => {
    const r = await post<{ item: Item }>(`/api/inbox/${e.id}/merge`, { item_id: item.id });
    drop([e.id]);
    toast(`Added as another photo of “${r.item.name}”.`);
  };

  const bulkConfirm = async () => {
    const ids = [...selected];
    const chosen = entries.filter((e) => ids.includes(e.id));
    const incomplete = chosen.filter((e) => missingTags(draftOf(e).tags).length > 0);
    const sendable = chosen.filter((e) => !incomplete.includes(e));
    if (!sendable.length) {
      toast({ text: "None of the selected photos have all their tags yet. Tap Edit on each first.", kind: "error" });
      return;
    }
    setBulkBusy(true);
    try {
      const res = await post<BulkResult>("/api/inbox/confirm", {
        entries: sendable.map((e) => {
          const d = draftOf(e);
          return { id: e.id, tags: cleanTags(d.tags), ...(d.name?.trim() ? { name: d.name.trim() } : {}) };
        }),
        include_matches: false,
      });
      drop(res.confirmed.map((c) => c.id));
      const parts = [`Added ${res.confirmed.length} to the closet.`];
      if (res.skipped.length) parts.push(`${res.skipped.length} ${res.skipped.length === 1 ? "looks like an item" : "look like items"} you own: merge, or add ${res.skipped.length === 1 ? "it" : "them"} one by one.`);
      if (res.failed.length) parts.push(`${res.failed.length} couldn't be added: check their tags.`);
      if (incomplete.length) parts.push(`${incomplete.length} still need tags.`);
      toast({ text: parts.join(" "), kind: res.failed.length ? "error" : "ok" });
      if (!res.skipped.length && !res.failed.length && !incomplete.length) setSelecting(false);
      setSelected(new Set([...res.skipped.map((s) => s.id), ...res.failed.map((f) => f.id), ...incomplete.map((e) => e.id)]));
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBulkBusy(false);
    }
  };

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const llmOff = health.data && !health.data.providers.llm;
  const keyMissing = entries.some(aiKeyMissing);

  return (
    <div className={`page ${selecting || entries.length ? "has-bottom-bar" : ""}`}>
      <div className="row-between">
        <h1 className="large-title">Inbox</h1>
        {entries.length > 0 && (
          <button
            className="btn plain"
            onClick={() => {
              setSelecting(!selecting);
              setSelected(new Set());
            }}
          >
            {selecting ? "Done" : "Select"}
          </button>
        )}
      </div>
      <p className="subtitle">{entries.length ? `${entries.length} photo${entries.length === 1 ? "" : "s"} to review. Nothing enters your closet until you confirm it.` : "New photos show up here once the mirror has processed them."}</p>

      {(llmOff || keyMissing) && (
        <Banner kind="warn">
          <b>Automatic tagging is off.</b> No AI key is set on the mirror (ANTHROPIC_API_KEY), so photos arrive without tags. You can tag them by hand below.
        </Banner>
      )}

      {inbox.error && !inbox.data ? (
        <LoadError error={inbox.error} retry={inbox.reload} />
      ) : !inbox.data ? (
        <Loading />
      ) : entries.length === 0 ? (
        <Empty icon="📥" title="All caught up" action={<button className="btn primary" onClick={() => navigate("/add")}>Add clothes</button>}>
          Photos you add are processed on the mirror and wait here for a quick check.
        </Empty>
      ) : (
        <div className="stack">
          {entries.map((e) => {
            const d = draftOf(e);
            const missing = missingTags(d.tags);
            const sel = selected.has(e.id);
            const isBusy = busy.has(e.id);
            const name = d.name || defaultName(d.tags) || "Untagged item";
            return (
              <div key={e.id} className={`card inbox-card ${sel ? "sel" : ""}`} onClick={selecting ? () => toggle(e.id) : undefined}>
                <div style={{ position: "relative" }}>
                  <Img src={e.cutout_url || e.photo_url} alt={name} />
                  {selecting && (
                    <div className="sel-overlay">
                      <div className={`select-dot ${sel ? "on" : ""}`}>{sel ? "✓" : ""}</div>
                    </div>
                  )}
                </div>
                <div className="body">
                  <div className="name ellipsis">{label(name)}</div>
                  <div className="chips">
                    {tagSummary(d.tags)
                      .slice(1)
                      .map((t) => (
                        <span className="chip" key={t}>
                          {t}
                        </span>
                      ))}
                    {e.origin === "checkin" && <span className="chip">From a check-in</span>}
                    {drafts[e.id] && <span className="chip accent">Edited</span>}
                    {missing.length > 0 && <span className="chip warn">Needs {missing.length === 6 ? "tags" : missing.join(", ").toLowerCase()}</span>}
                    {e.problem && <span className="chip bad">Background not removed</span>}
                  </div>
                  {e.match && (
                    <div className="tiny" style={{ color: "var(--warn)" }}>
                      Looks like “{e.match.item.name}” you already own
                    </div>
                  )}
                  {!selecting && (
                    <div className="actions">
                      {e.match ? (
                        <button className="btn small tinted" disabled={isBusy} onClick={() => setMerging(e)}>
                          Merge
                        </button>
                      ) : null}
                      <button className="btn small" disabled={isBusy} onClick={() => setEditing(e)}>
                        Edit
                      </button>
                      <button
                        className="btn small primary"
                        disabled={isBusy}
                        onClick={() =>
                          missing.length
                            ? setEditing(e)
                            : void withBusy(e.id, async () => {
                                try {
                                  await confirmOne(e);
                                } catch (err) {
                                  if (err instanceof ApiError && err.status === 422) setEditing(e);
                                  toast({ text: errorMessage(err), kind: "error" });
                                }
                              })
                        }
                      >
                        {isBusy ? <Spinner small /> : "Add"}
                      </button>
                      <button className="btn small icon danger" aria-label="Reject" disabled={isBusy} onClick={() => void reject(e)}>
                        🗑
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {entries.length > 0 && (
        <div className="bottom-bar">
          {selecting ? (
            <>
              <button className="btn big" onClick={() => setSelected(selected.size === ready.length ? new Set() : new Set(ready.map((e) => e.id)))}>
                {selected.size && selected.size === ready.length ? "Clear" : `Select ready (${ready.length})`}
              </button>
              <button className="btn big primary" disabled={!selected.size || bulkBusy} onClick={() => void bulkConfirm()}>
                {bulkBusy ? <Spinner small /> : `Add ${selected.size || ""} to closet`}
              </button>
            </>
          ) : (
            <button
              className="btn big primary"
              disabled={!ready.length}
              onClick={() => {
                setSelecting(true);
                setSelected(new Set(ready.map((e) => e.id)));
              }}
            >
              {ready.length ? `Add all ready (${ready.length})…` : "Tag photos to add them"}
            </button>
          )}
        </div>
      )}

      {editing && (
        <EditSheet
          entry={editing}
          draft={draftOf(editing)}
          onClose={() => setEditing(null)}
          onSave={(d) => setDrafts((x) => ({ ...x, [editing.id]: d }))}
          onConfirm={async (d) => {
            setDrafts((x) => ({ ...x, [editing.id]: d }));
            await confirmOne(editing, d);
          }}
          onMerge={() => {
            setMerging(editing);
            setEditing(null);
          }}
        />
      )}
      {merging && <MergeSheet entry={merging} onClose={() => setMerging(null)} onMerge={(i) => merge(merging, i)} />}
    </div>
  );
}
