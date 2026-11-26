// API types. Request bodies and the routes that declare a response model come from the
// generated OpenAPI schema (schema.d.ts, `npm run gen:api`). Many routes return a plain
// dict, so OpenAPI only says "object" for them; those response shapes are written here
// from the router code and marked HAND. Ask the backend for response_model on those
// routes and these can be replaced by generated types.
import type { components } from "./schema";
import type { CATEGORIES } from "./taxonomy.gen";

type S = components["schemas"];

export type TodayPayload = S["TodayPayload"] & {
  /** Additive field being added by the backend: names of stale sources. */
  stale?: string[];
};
export type WeatherSnapshot = S["WeatherSnapshot"];
export type CalendarEvent = S["CalendarEvent"];
export type Reminder = S["Reminder"];
export type Device = S["Device"];
export type PairRequest = S["PairRequest"];
export type PairResponse = S["PairResponse"];
export type MintResponse = S["MintResponse"];
export type RemindersResponse = S["RemindersResponse"];
export type FeedbackBody = S["FeedbackBody"];
export type BulkBody = S["BulkBody"];
export type InboxConfirmBody = S["mirror_api__routers__inbox__ConfirmBody"];
export type InboxMergeBody = S["mirror_api__routers__inbox__MergeBody"];
export type ItemMergeBody = S["mirror_api__routers__items__MergeBody"];
export type ItemStateBody = S["StateBody"];
export type CheckinConfirmBody = S["mirror_api__routers__checkins__ConfirmBody"];
export type ProfileBody = S["ProfileBody"];

export type Category = (typeof CATEGORIES)[number];
export type ItemState = ItemStateBody["state"];

/** Taxonomy fields of an item (validated server-side by taxonomy.validate_tags). */
export interface Tags {
  category?: string | null;
  subcategory?: string | null;
  primary_color?: string | null;
  secondary_color?: string | null;
  pattern?: string | null;
  material_guess?: string | null;
  formality?: number | null;
  season?: string[] | null;
  fit?: string | null;
  style_tags?: string[] | null;
}

/** HAND: pipeline.closet.item_out */
export interface Item {
  id: string;
  name: string;
  tags: Tags;
  state: ItemState;
  last_worn_on: string | null;
  cutout_url: string | null;
  source_photo_id: string | null;
  has_embedding: boolean;
  created_at: string;
  updated_at: string;
}

/** HAND: GET /api/items */
export interface ItemList {
  total: number;
  items: Item[];
}

export interface Match {
  item: Item;
  score: number | null;
  kind: string | null; // "duplicate" | "possible" | ...
  method: string | null;
  candidates: { item_id?: string; id?: string; score?: number; name?: string }[];
}

/** HAND: GET /api/inbox entries */
export interface InboxEntry {
  id: string;
  photo_id: string;
  proposed_tags: Tags & { name?: string };
  confidence: number | null;
  tagging: string; // "ok" | "partial" | "failed" | "unknown"
  tag_errors: string[];
  needs_review: boolean;
  problem: string | null;
  cutout_url: string | null;
  photo_url: string | null;
  match: Match | null;
  created_at: string;
  /** "onboarding" (a flat photo) or "checkin" (sent from a mirror/phone check-in). */
  origin?: string;
}
export interface Inbox {
  count: number;
  entries: InboxEntry[];
}
/** HAND: POST /api/inbox/confirm */
export interface BulkResult {
  confirmed: { id: string; item: Item }[];
  skipped: { id: string; reason: string; item_id: string }[];
  failed: { id: string; status: number; message: string; errors: string[] }[];
}

/** HAND: pipeline.uploads.photo_status (+ duplicate_upload on upload) */
export interface PhotoStatus {
  photo_id: string;
  source: string;
  status: "queued" | "processing" | "done" | "failed" | string;
  step: string | null;
  error: string | null;
  width: number | null;
  height: number | null;
  uploaded_at: string;
  duplicate_upload?: boolean;
}

/** HAND: GET /api/outfits, POST /api/outfits/reroll */
export interface OutfitView {
  id: string;
  for_date: string;
  rank: number;
  status: string;
  source: "llm" | "rules" | string;
  reasoning: string;
  items: {
    item_id: string;
    slot: string;
    name: string | null;
    category: string | null;
    subcategory: string | null;
    color: string | null;
    state: string | null;
    cutout_url: string | null;
  }[];
  collage_key: string | null;
  collage_url: string | null;
}
export interface OutfitsPayload {
  date: string;
  outfits: OutfitView[];
  message: string | null;
  notes: string[];
  source?: string;
  fallback_reason?: string | null;
}

/** HAND: GET /api/checkins/{photo_id} */
export interface CheckinGarment {
  detection_id: string;
  label: string | null;
  box: unknown;
  tags: Tags | null;
  status: string;
  segmentation: string | null;
  cutout_url: string | null;
  match: Match | null;
  /** Set once this garment has been sent to the review inbox. */
  inbox_id?: string | null;
}
export interface Checkin extends PhotoStatus {
  photo_url: string | null;
  garments: CheckinGarment[];
}

/** HAND: recommender.style.StyleProfile.as_dict */
export interface StyleProfile {
  version: number | null;
  text: string;
  fit_notes: string | null;
  quiz: Record<string, unknown>;
  source: string;
  updated_at: string | null;
}
export interface QuizQuestion {
  id: string;
  kind: "single" | "multi" | "scale" | "text";
  prompt: string;
  required: boolean;
  options?: { value: string; label: string }[];
  max_choices?: number;
  min?: number;
  max?: number;
  max_length?: number;
}

/** HAND: GET /api/health */
export interface Health {
  status: "ok" | "degraded";
  app_env: string;
  db: { configured: boolean; ok: boolean; error?: string };
  storage: { configured: boolean; backend: string; ok: boolean; error?: string };
  spend: null | {
    ledger: string;
    mode: "normal" | "degraded" | "blocked";
    percent: number;
    spent_usd: number;
    cap_usd: number;
  };
  providers: {
    llm: boolean;
    fal: boolean;
    weather: boolean;
    timezone: boolean;
    calendars: Record<string, boolean>;
  };
  hardware: string;
  ws_clients: { mirror: number; phone: number };
}

/** HAND: GET /api/today/status */
export interface IntegrationStatus {
  date: string;
  weather: { configured: boolean; last_success: string | null; last_error: string | null; stale: boolean };
  calendars: Record<string, unknown>;
  reminders: { last_push: string | null; count: number };
  outfits: { last_sync: string | null; today: number };
  formality: { cached_titles: number; llm_configured: boolean };
  spend_mode: string;
}

/** Capture contract (being added by the backend; see HANDOFF section 9). */
export type CaptureState =
  | "idle"
  | "cover_wait"
  | "preview"
  | "countdown"
  | "flash"
  | "quality_check"
  | "review"
  | "cover_reminder";
export interface CaptureStatus {
  capture_id?: string;
  state: CaptureState;
  message?: string | null;
  seconds_left?: number | null;
  review_url?: string | null;
  /** Not in the contract; used if the backend adds it to keep's response or the event. */
  photo_id?: string | null;
}

/** WebSocket envelope. */
export interface WsMessage {
  type: string;
  data?: unknown;
  at?: string;
}
