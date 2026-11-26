import { useEffect, useState } from "react";
import { api } from "../api/client";
import * as T from "../api/taxonomy.gen";
import type { ItemList, ItemState } from "../api/types";
import { Empty, Img, LoadError, label } from "../components/ui";
import { useLoad } from "../lib/hooks";
import { navigate } from "../lib/router";

const VIEWS: { key: string; label: string; states: ItemState[] }[] = [
  { key: "all", label: "All", states: ["clean", "dirty"] },
  { key: "clean", label: "Clean", states: ["clean"] },
  { key: "dirty", label: "Laundry", states: ["dirty"] },
  { key: "retired", label: "Retired", states: ["retired"] },
];

function saved<T>(k: string, d: T): T {
  try {
    const v = sessionStorage.getItem(k);
    return v ? (JSON.parse(v) as T) : d;
  } catch {
    return d;
  }
}

export function Closet() {
  const [cat, setCat] = useState<string>(() => saved("closet.cat", ""));
  const [view, setView] = useState<string>(() => saved("closet.view", "all"));
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => {
    try {
      sessionStorage.setItem("closet.cat", JSON.stringify(cat));
      sessionStorage.setItem("closet.view", JSON.stringify(view));
    } catch {
      /* ignore */
    }
  }, [cat, view]);

  const states = VIEWS.find((v) => v.key === view)!.states;
  const params = new URLSearchParams({ limit: "500" });
  if (cat) params.set("category", cat);
  if (query) params.set("q", query);
  states.forEach((s) => params.append("state", s));
  const items = useLoad(() => api<ItemList>(`/api/items?${params}`), [params.toString()], ["items.changed", "inbox.updated"]);
  const list = items.data?.items ?? [];

  return (
    <div className="page">
      <h1 className="large-title">Closet</h1>
      <input className="input" type="search" placeholder="Search your clothes" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 12 }} />
      <div className="scroll-chips" role="tablist" aria-label="Category">
        {["", ...T.CATEGORIES].map((c) => (
          <button key={c || "all"} role="tab" aria-selected={cat === c} className={`filter-chip ${cat === c ? "on" : ""}`} onClick={() => setCat(c)}>
            {c ? label(c === "one_piece" ? "dresses & one-pieces" : c) : "Everything"}
          </button>
        ))}
      </div>
      <div className="segmented" style={{ margin: "4px 0 16px" }}>
        {VIEWS.map((v) => (
          <button key={v.key} className={view === v.key ? "on" : ""} onClick={() => setView(v.key)}>
            {v.label}
          </button>
        ))}
      </div>

      {items.error && !items.data ? (
        <LoadError error={items.error} retry={items.reload} />
      ) : !items.data ? (
        <div className="grid three">
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className="skeleton" style={{ aspectRatio: "3 / 4.6" }} />
          ))}
        </div>
      ) : list.length === 0 ? (
        query || cat || view !== "all" ? (
          <Empty icon="🔍" title="Nothing here">
            No items match these filters.
          </Empty>
        ) : (
          <Empty icon="🧺" title="Your closet is empty" action={<button className="btn primary" onClick={() => navigate("/add")}>Add your first clothes</button>}>
            Photograph each garment on a plain sheet. The mirror cuts it out and tags it; you confirm it in the inbox.
          </Empty>
        )
      ) : (
        <>
          <div className="tiny" style={{ margin: "0 4px 8px" }}>
            {items.data.total} item{items.data.total === 1 ? "" : "s"}
          </div>
          <div className="grid three">
            {list.map((i) => (
              <button key={i.id} className={`tile ${i.state !== "clean" ? "dim" : ""}`} onClick={() => navigate(`/closet/${i.id}`)}>
                <Img src={i.cutout_url} alt={i.name} fallback={label(i.tags.subcategory)} />
                <div className="cap">
                  <div className="ellipsis">{label(i.name)}</div>
                  <div className="tiny ellipsis">{i.state === "dirty" ? "In the laundry" : i.state === "retired" ? "Retired" : label(i.tags.subcategory)}</div>
                </div>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
