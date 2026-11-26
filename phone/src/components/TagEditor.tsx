import * as T from "../api/taxonomy.gen";
import type { Tags } from "../api/types";
import { label } from "./ui";

export const REQUIRED: (keyof Tags)[] = ["category", "subcategory", "primary_color", "pattern", "material_guess", "formality"];

export function missingTags(t: Tags): string[] {
  return REQUIRED.filter((k) => t[k] === null || t[k] === undefined || t[k] === "").map((k) => label(k === "material_guess" ? "material" : k));
}

/** Only the taxonomy fields, in the shape the API accepts. */
export function cleanTags(t: Tags): Tags {
  const out: Tags = {};
  for (const k of ["category", "subcategory", "primary_color", "secondary_color", "pattern", "material_guess", "formality", "season", "fit", "style_tags"] as const) {
    const v = t[k];
    if (v === undefined) continue;
    (out as Record<string, unknown>)[k] = v === "" ? null : v;
  }
  return out;
}

function Select({
  name,
  value,
  options,
  onChange,
  optional,
  labels,
}: {
  name: string;
  value: string | number | null | undefined;
  options: readonly (string | number)[];
  onChange: (v: string | null) => void;
  optional?: boolean;
  labels?: Record<string, string>;
}) {
  return (
    <label className="field">
      <span>
        {name}
        {optional ? "" : " *"}
      </span>
      <select className="input" value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{optional ? "None" : "Choose…"}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {labels?.[String(o)] ?? label(String(o))}
          </option>
        ))}
      </select>
    </label>
  );
}

function Multi({
  name,
  values,
  options,
  max,
  onChange,
}: {
  name: string;
  values: string[];
  options: readonly string[];
  max?: number;
  onChange: (v: string[]) => void;
}) {
  return (
    <div className="field">
      <span>
        {name}
        {max ? ` (up to ${max})` : ""}
      </span>
      <div className="toggle-grid">
        {options.map((o) => {
          const on = values.includes(o);
          return (
            <button
              type="button"
              key={o}
              className={`toggle ${on ? "on" : ""}`}
              aria-pressed={on}
              onClick={() => {
                if (on) onChange(values.filter((x) => x !== o));
                else if (!max || values.length < max) onChange([...values, o]);
              }}
            >
              {label(o)}
            </button>
          );
        })}
      </div>
    </div>
  );
}

const FORMALITY_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(T.FORMALITY).map(([k, v]) => [k, `${k} · ${v.split(":")[0]}`]),
);

export function TagEditor({
  tags,
  name,
  onChange,
  onName,
}: {
  tags: Tags;
  name?: string;
  onChange: (t: Tags) => void;
  onName?: (n: string) => void;
}) {
  const set = (k: keyof Tags, v: unknown) => onChange({ ...tags, [k]: v });
  const cat = (tags.category ?? "") as keyof typeof T.SUBCATEGORIES | "";
  const subs = cat ? T.SUBCATEGORIES[cat] : [];
  return (
    <div>
      {onName && (
        <label className="field">
          <span>Name</span>
          <input className="input" value={name ?? ""} maxLength={60} placeholder="e.g. Navy oxford shirt" onChange={(e) => onName(e.target.value)} />
        </label>
      )}
      <div className="row" style={{ gap: 10, alignItems: "start" }}>
        <div className="grow">
          <Select
            name="Category"
            value={tags.category}
            options={T.CATEGORIES}
            onChange={(v) => onChange({ ...tags, category: v, subcategory: v && (T.SUBCATEGORIES as Record<string, readonly string[]>)[v]?.includes(tags.subcategory ?? "") ? tags.subcategory : null })}
          />
        </div>
        <div className="grow">
          <Select name="Type" value={tags.subcategory} options={subs} onChange={(v) => set("subcategory", v)} />
        </div>
      </div>
      <div className="row" style={{ gap: 10, alignItems: "start" }}>
        <div className="grow">
          <Select name="Main colour" value={tags.primary_color} options={T.COLORS} onChange={(v) => set("primary_color", v)} />
        </div>
        <div className="grow">
          <Select name="Second colour" optional value={tags.secondary_color} options={T.COLORS} onChange={(v) => set("secondary_color", v)} />
        </div>
      </div>
      <div className="row" style={{ gap: 10, alignItems: "start" }}>
        <div className="grow">
          <Select name="Pattern" value={tags.pattern} options={T.PATTERNS} onChange={(v) => set("pattern", v)} />
        </div>
        <div className="grow">
          <Select name="Material" value={tags.material_guess} options={T.MATERIALS} onChange={(v) => set("material_guess", v)} />
        </div>
      </div>
      <div className="row" style={{ gap: 10, alignItems: "start" }}>
        <div className="grow">
          <Select
            name="Formality"
            value={tags.formality}
            options={Object.keys(T.FORMALITY)}
            labels={FORMALITY_LABELS}
            onChange={(v) => set("formality", v ? Number(v) : null)}
          />
        </div>
        <div className="grow">
          <Select name="Fit" optional value={tags.fit} options={T.FITS} onChange={(v) => set("fit", v)} />
        </div>
      </div>
      <Multi name="Seasons (none = all year)" values={tags.season ?? []} options={T.SEASONS} onChange={(v) => set("season", v)} />
      <Multi name="Style" values={tags.style_tags ?? []} options={T.STYLE_TAGS} max={T.MAX_STYLE_TAGS} onChange={(v) => set("style_tags", v)} />
    </div>
  );
}

export function tagSummary(t: Tags | null | undefined): string[] {
  if (!t) return [];
  return [t.primary_color, t.subcategory || t.category, t.pattern && t.pattern !== "solid" ? t.pattern : null, t.material_guess && t.material_guess !== "unknown" ? t.material_guess : null]
    .filter(Boolean)
    .map((x) => label(String(x)));
}
