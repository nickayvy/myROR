import { useState } from "react";
import { ApiError, api, del, errorMessage, patch, post } from "../api/client";
import type { Item, ItemList, ItemState, Tags } from "../api/types";
import { cleanTags, missingTags, TagEditor, tagSummary } from "../components/TagEditor";
import { Img, LoadError, Loading, Nav, Sheet, Spinner, label } from "../components/ui";
import { toast, useLoad } from "../lib/hooks";
import { navigate } from "../lib/router";

function MergeInto({ item, onClose }: { item: Item; onClose: () => void }) {
  const list = useLoad(() => api<ItemList>(`/api/items?limit=200&category=${encodeURIComponent(item.tags.category || "")}`), [item.id]);
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <Sheet title="Merge into…" onClose={onClose}>
      <p className="small muted" style={{ marginTop: 0 }}>
        Two entries for the same garment? Pick the one to keep. Wear history and photos from “{item.name}” move to it.
      </p>
      <div className="list">
        {!list.data ? (
          <Loading />
        ) : (
          list.data.items
            .filter((i) => i.id !== item.id)
            .map((i) => (
              <button
                key={i.id}
                className="list-row"
                disabled={!!busy}
                onClick={async () => {
                  setBusy(i.id);
                  try {
                    const r = await post<{ item: Item }>(`/api/items/${item.id}/merge`, { into: i.id });
                    toast(`Merged into “${r.item.name}”.`);
                    navigate(`/closet/${r.item.id}`, { replace: true });
                  } catch (e) {
                    toast({ text: errorMessage(e), kind: "error" });
                  } finally {
                    setBusy(null);
                  }
                }}
              >
                <Img src={i.cutout_url} alt="" style={{ width: 48, height: 60, borderRadius: 8, flex: "none" }} />
                <div className="grow">
                  <div className="ellipsis" style={{ fontWeight: 500 }}>
                    {i.name}
                  </div>
                  <div className="tiny">{tagSummary(i.tags).join(" · ")}</div>
                </div>
                {busy === i.id && <Spinner small />}
              </button>
            ))
        )}
      </div>
    </Sheet>
  );
}

export function ItemDetail({ id }: { id: string }) {
  const item = useLoad(() => api<Item>(`/api/items/${id}`), [id]);
  const [editing, setEditing] = useState(false);
  const [tags, setTags] = useState<Tags>({});
  const [name, setName] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [merging, setMerging] = useState(false);

  const i = item.data;
  if (item.error && !i) {
    return (
      <div>
        <Nav title="Item" backTo="/closet" />
        <LoadError error={item.error} retry={item.reload} />
      </div>
    );
  }
  if (!i)
    return (
      <div>
        <Nav title="Item" backTo="/closet" />
        <Loading />
      </div>
    );

  const run = async (fn: () => Promise<Item | void>, ok?: string) => {
    setBusy(true);
    try {
      const r = await fn();
      if (r) item.setData(() => r);
      if (ok) toast(ok);
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  };
  const setState = (state: ItemState, msg: string) => run(() => post<Item>(`/api/items/${i.id}/state`, { state }), msg);

  const save = async () => {
    setBusy(true);
    setErrors([]);
    try {
      const r = await patch<Item>(`/api/items/${i.id}`, { ...cleanTags(tags), name: name.trim() || i.name });
      item.setData(() => r);
      setEditing(false);
      toast("Saved.");
    } catch (e) {
      setErrors(e instanceof ApiError && e.errors.length ? e.errors : [errorMessage(e)]);
    } finally {
      setBusy(false);
    }
  };

  const t = i.tags;
  const rows: [string, string][] = [
    ["Category", label(t.category)],
    ["Type", label(t.subcategory)],
    ["Colour", [t.primary_color, t.secondary_color].filter(Boolean).map((c) => label(c)).join(" / ")],
    ["Pattern", label(t.pattern)],
    ["Material", label(t.material_guess)],
    ["Formality", t.formality ? String(t.formality) : ""],
    ["Fit", label(t.fit)],
    ["Seasons", t.season?.length ? t.season.map(label).join(", ") : "All year"],
    ["Style", (t.style_tags ?? []).map(label).join(", ")],
    ["Last worn", i.last_worn_on ? new Date(i.last_worn_on + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" }) : "Not yet"],
  ];

  return (
    <div>
      <Nav
        title={editing ? "Edit item" : label(i.name)}
        backTo="/closet"
        left={editing ? <button className="btn plain" onClick={() => setEditing(false)}>Cancel</button> : undefined}
        right={
          editing ? (
            <button className="btn plain" style={{ fontWeight: 600 }} disabled={busy || missingTags(tags).length > 0} onClick={() => void save()}>
              {busy ? <Spinner small /> : "Save"}
            </button>
          ) : (
            <button
              className="btn plain"
              onClick={() => {
                setTags(i.tags);
                setName(i.name);
                setErrors([]);
                setEditing(true);
              }}
            >
              Edit
            </button>
          )
        }
      />
      <div className="page" style={{ paddingTop: 12 }}>
        {editing ? (
          <div className="card pad">
            <TagEditor tags={tags} name={name} onChange={setTags} onName={setName} />
            {errors.map((e) => (
              <div className="field-error" key={e}>
                {e}
              </div>
            ))}
          </div>
        ) : (
          <>
            <Img className="hero" src={i.cutout_url} alt={i.name} />
            <div className="row-between mt">
              <div className="grow">
                <div style={{ fontSize: 22, fontWeight: 700 }}>{label(i.name)}</div>
                <div className="small muted">{i.state === "dirty" ? "In the laundry" : i.state === "retired" ? "Retired" : "Clean and ready"}</div>
              </div>
            </div>
            <div className="btn-row mt">
              {i.state === "dirty" ? (
                <button className="btn tinted" disabled={busy} onClick={() => void setState("clean", "Marked clean.")}>
                  🧺 Mark clean
                </button>
              ) : i.state === "clean" ? (
                <button className="btn" disabled={busy} onClick={() => void setState("dirty", "Moved to laundry.")}>
                  🧺 Laundry
                </button>
              ) : (
                <button className="btn tinted" disabled={busy} onClick={() => void setState("clean", "Back in the closet.")}>
                  Restore
                </button>
              )}
              <button
                className="btn primary"
                disabled={busy || i.state === "retired"}
                onClick={() =>
                  void run(async () => {
                    const r = await post<{ logged: boolean }>(`/api/items/${i.id}/worn`, {});
                    toast(r.logged ? "Logged as worn today." : "Already logged for today.");
                    return api<Item>(`/api/items/${i.id}`);
                  })
                }
              >
                Wore it today
              </button>
            </div>
            <div className="card pad mt2">
              <dl className="kv" style={{ margin: 0 }}>
                {rows.map(([k, v]) => (
                  <div key={k} style={{ display: "contents" }}>
                    <dt>{k}</dt>
                    <dd>{v || "–"}</dd>
                  </div>
                ))}
              </dl>
            </div>
            <div className="list mt2">
              <button className="list-row chev" onClick={() => setMerging(true)}>
                Duplicate of another item? Merge
              </button>
              {i.state !== "retired" && (
                <button
                  className="list-row"
                  style={{ color: "var(--bad)" }}
                  disabled={busy}
                  onClick={() => {
                    if (confirm(`Retire “${i.name}”? It stays in your history but won't be suggested again. You can restore it later.`)) void setState("retired", "Retired.");
                  }}
                >
                  Retire item
                </button>
              )}
              {i.source_photo_id && (
                <button
                  className="list-row"
                  style={{ color: "var(--bad)" }}
                  disabled={busy}
                  onClick={() => {
                    if (!confirm("Delete this item and its photo permanently? The photo and cutout are removed from storage. This can't be undone.")) return;
                    void run(async () => {
                      await del(`/api/photos/${i.source_photo_id}?delete_items=true`);
                      toast("Deleted.");
                      navigate("/closet", { replace: true });
                    });
                  }}
                >
                  Delete item and photo
                </button>
              )}
            </div>
          </>
        )}
      </div>
      {merging && <MergeInto item={i} onClose={() => setMerging(false)} />}
    </div>
  );
}
