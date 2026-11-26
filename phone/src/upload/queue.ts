// Bulk upload queue: one file per request, a few in parallel, per-photo progress,
// automatic retry with backoff, and resume after a reload, sleep or backgrounding.
//
// Every queued file is kept in IndexedDB until the server has it, so a killed tab picks up
// where it left off. Re-sending is safe: the server dedupes identical bytes by SHA-256
// (200 + duplicate_upload instead of 201). After upload, pipeline progress comes from
// `pipeline.photo` WebSocket events, with a status fetch on (re)connect as a fallback.
import { useSyncExternalStore } from "react";
import { api, getToken } from "../api/client";
import { onWs } from "../api/ws";
import type { PhotoStatus } from "../api/types";
import { idbAll, idbDel, idbPut } from "./idb";

export type UploadState = "waiting" | "uploading" | "uploaded" | "failed";
export interface Upload {
  id: string;
  name: string;
  size: number;
  type: string;
  addedAt: number;
  blob?: Blob; // dropped once the server has the file
  thumb?: string; // small data: URL so the list keeps a picture after the blob is gone
  state: UploadState;
  progress: number; // 0..1 while uploading
  attempts: number;
  nextTryAt?: number;
  error?: string;
  permanent?: boolean; // failed in a way retrying will not fix
  photoId?: string;
  duplicate?: boolean;
  pipeline?: { status: string; step: string | null; error: string | null };
}

const CONCURRENCY = 3;
const MAX_AUTO_ATTEMPTS = 8;
const UPLOAD_TIMEOUT_MS = 120_000;
const ENDPOINT = "/api/items/upload";

let items: Upload[] = [];
let loaded = false;
const active = new Map<string, XMLHttpRequest>();
const subs = new Set<() => void>();
let snapshot: Upload[] = [];
let wakeTimer: ReturnType<typeof setTimeout> | undefined;

function emit() {
  snapshot = items.slice();
  subs.forEach((s) => s());
}
function persist(u: Upload) {
  void idbPut(u.id, u);
}
function update(id: string, patch: Partial<Upload>, save = true) {
  const i = items.findIndex((u) => u.id === id);
  if (i < 0) return;
  items[i] = { ...items[i], ...patch };
  if (save) persist(items[i]);
  emit();
}

export function useUploads(): Upload[] {
  return useSyncExternalStore(
    (cb) => {
      subs.add(cb);
      return () => subs.delete(cb);
    },
    () => snapshot,
  );
}

async function thumbnail(blob: Blob): Promise<string | undefined> {
  try {
    const url = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.decoding = "async";
      img.src = url;
      await img.decode();
      const s = 112 / Math.max(img.naturalWidth, img.naturalHeight);
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(img.naturalWidth * s));
      c.height = Math.max(1, Math.round(img.naturalHeight * s));
      c.getContext("2d")!.drawImage(img, 0, 0, c.width, c.height);
      return c.toDataURL("image/jpeg", 0.7);
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch {
    return undefined; // e.g. HEIC the browser can't decode: the list shows a placeholder
  }
}

function uid(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

export async function addFiles(files: FileList | File[]): Promise<void> {
  await init();
  const now = Date.now();
  const added: Upload[] = [];
  for (const f of Array.from(files)) {
    // Copy into a Blob we own: on iOS a File from the picker can become unreadable later.
    const blob = new Blob([await f.arrayBuffer()], { type: f.type || "image/jpeg" });
    const u: Upload = {
      id: uid(),
      name: f.name || "photo.jpg",
      size: f.size,
      type: f.type || "image/jpeg",
      addedAt: now + added.length,
      blob,
      thumb: await thumbnail(blob),
      state: "waiting",
      progress: 0,
      attempts: 0,
    };
    added.push(u);
    persist(u);
    // Show and start each photo as soon as it is read, not after the whole batch.
    items = [...items, u];
    emit();
    pump();
  }
}

export function retry(id: string): void {
  const u = items.find((x) => x.id === id);
  if (!u) return;
  if (u.state === "failed" && u.blob) {
    update(id, { state: "waiting", attempts: 0, error: undefined, permanent: false, nextTryAt: undefined });
    pump();
  } else if (u.photoId && u.pipeline?.status === "failed") {
    // The server has the file; ask it to re-run the failed steps.
    update(id, { pipeline: { status: "queued", step: null, error: null } });
    api(`/api/photos/${u.photoId}/retry`, { method: "POST" }).catch((e) =>
      update(id, { pipeline: { status: "failed", step: null, error: String((e as Error).message) } }),
    );
  }
}

export function retryAllFailed(): void {
  items.filter((u) => u.state === "failed" || u.pipeline?.status === "failed").forEach((u) => retry(u.id));
}

export function remove(id: string): void {
  active.get(id)?.abort();
  active.delete(id);
  items = items.filter((u) => u.id !== id);
  void idbDel(id);
  emit();
  pump();
}

export function clearFinished(): void {
  for (const u of items.filter(isFinished)) void idbDel(u.id);
  items = items.filter((u) => !isFinished(u));
  emit();
}

export function isFinished(u: Upload): boolean {
  return u.state === "uploaded" && u.pipeline?.status === "done";
}

function pump() {
  clearTimeout(wakeTimer);
  if (!getToken()) return;
  const now = Date.now();
  let running = active.size;
  let soonest = Infinity;
  for (const u of items) {
    if (running >= CONCURRENCY) break;
    if (u.state !== "waiting" || active.has(u.id)) continue;
    if (u.nextTryAt && u.nextTryAt > now) {
      soonest = Math.min(soonest, u.nextTryAt);
      continue;
    }
    running++;
    send(u);
  }
  if (soonest < Infinity) wakeTimer = setTimeout(pump, soonest - now + 20);
}

function backoff(attempts: number): number {
  return Math.min(30_000, 1000 * 2 ** (attempts - 1)) * (0.75 + Math.random() * 0.5);
}

function send(u: Upload) {
  if (!u.blob) {
    update(u.id, { state: "failed", error: "The photo is no longer on this phone. Add it again.", permanent: true });
    return;
  }
  const xhr = new XMLHttpRequest();
  active.set(u.id, xhr);
  update(u.id, { state: "uploading", progress: 0, error: undefined }, false);
  const form = new FormData();
  form.append("file", u.blob, u.name);
  xhr.open("POST", ENDPOINT);
  xhr.timeout = UPLOAD_TIMEOUT_MS;
  const token = getToken();
  if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
  let lastEmit = 0;
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const now = Date.now();
    if (now - lastEmit < 120 && e.loaded < e.total) return;
    lastEmit = now;
    update(u.id, { progress: e.loaded / e.total }, false);
  };
  const transient = (why: string) => {
    active.delete(u.id);
    const attempts = u.attempts + 1;
    const offline = !navigator.onLine || document.visibilityState === "hidden";
    if (!offline && attempts >= MAX_AUTO_ATTEMPTS) {
      update(u.id, { state: "failed", progress: 0, attempts, error: why });
    } else {
      // Offline or backgrounded: don't burn attempts, just wait for the page to wake.
      const a = offline ? u.attempts : attempts;
      update(u.id, { state: "waiting", progress: 0, attempts: a, error: why, nextTryAt: Date.now() + backoff(Math.max(1, a)) });
    }
    pump();
  };
  xhr.onerror = () => transient("Connection lost. Retrying…");
  xhr.ontimeout = () => transient("Timed out. Retrying…");
  xhr.onabort = () => active.delete(u.id);
  xhr.onload = () => {
    active.delete(u.id);
    let body: unknown;
    try {
      body = JSON.parse(xhr.responseText);
    } catch {
      body = undefined;
    }
    if (xhr.status === 200 || xhr.status === 201) {
      const p = body as PhotoStatus;
      update(u.id, {
        state: "uploaded",
        progress: 1,
        blob: undefined, // the server has it now
        photoId: p.photo_id,
        duplicate: !!p.duplicate_upload,
        error: undefined,
        pipeline: { status: p.status, step: p.step, error: p.error },
      });
      schedulePoll();
      pump();
      return;
    }
    const detail = (body as { detail?: { message?: string; code?: string } | string } | undefined)?.detail;
    const msg = typeof detail === "string" ? detail : detail?.message;
    if (xhr.status === 401) {
      update(u.id, { state: "waiting", error: "Not paired. Pair the phone again to continue." });
      return; // pump() resumes after pairing
    }
    if (xhr.status >= 500 || xhr.status === 0 || xhr.status === 429 || xhr.status === 408) {
      transient(xhr.status === 503 ? msg || "The mirror is busy. Retrying…" : `Server error (${xhr.status}). Retrying…`);
      return;
    }
    const reason =
      xhr.status === 413
        ? "This photo is too large."
        : xhr.status === 415
          ? "This file type isn't supported. Use a photo (JPEG, PNG or HEIC)."
          : xhr.status === 409
            ? "This exact photo was already uploaded as a check-in."
            : msg || `Rejected (${xhr.status}).`;
    update(u.id, { state: "failed", progress: 0, attempts: u.attempts + 1, error: reason, permanent: true });
    pump();
  };
  xhr.send(form);
}

// Fallback for missed `pipeline.photo` events (e.g. an event that raced the upload reply):
// while any uploaded photo is still processing and the page is visible, re-check every 5 s.
// Stops by itself when nothing is pending, so an idle phone never polls.
let pollTimer: ReturnType<typeof setTimeout> | undefined;
function schedulePoll() {
  clearTimeout(pollTimer);
  const pending = items.some((u) => u.photoId && u.pipeline && !["done", "failed"].includes(u.pipeline.status));
  if (!pending) return;
  pollTimer = setTimeout(() => {
    if (document.visibilityState === "visible") void refreshPipeline();
    else schedulePoll();
  }, 5000);
}

async function refreshPipeline() {
  const pending = items.filter((u) => u.photoId && u.pipeline?.status !== "done");
  if (!pending.length) return;
  schedulePoll();
  try {
    const res = await api<{ photos: PhotoStatus[] }>("/api/photos?source=phone_onboarding&limit=500");
    const byId = new Map(res.photos.map((p) => [p.photo_id, p]));
    for (const u of pending) {
      const p = byId.get(u.photoId!);
      if (p) update(u.id, { pipeline: { status: p.status, step: p.step, error: p.error } });
    }
  } catch {
    /* try again on the next reconnect */
  }
}

let initp: Promise<void> | null = null;
export function init(): Promise<void> {
  if (initp) return initp;
  initp = (async () => {
    const saved = await idbAll<Upload>();
    if (!loaded) {
      loaded = true;
      // Anything mid-upload when the page died starts again (the server dedupes).
      items = saved
        .map((u) => (u.state === "uploading" ? { ...u, state: "waiting" as const, progress: 0 } : u))
        .sort((a, b) => a.addedAt - b.addedAt);
      emit();
    }
    onWs("pipeline.photo", (m) => {
      const d = m.data as { photo_id: string; status: string; step: string | null; error: string | null };
      const u = items.find((x) => x.photoId === d.photo_id);
      if (u) update(u.id, { pipeline: { status: d.status, step: d.step, error: d.error } });
    });
    onWs("inbox.updated", (m) => {
      const d = m.data as { photo_id?: string };
      const u = items.find((x) => x.photoId === d.photo_id);
      if (u) update(u.id, { pipeline: { status: "done", step: null, error: null } });
    });
    const wake = () => {
      if (document.visibilityState !== "visible") return;
      for (const u of items) if (u.state === "waiting") u.nextTryAt = undefined;
      pump();
    };
    onWs("connected", () => void refreshPipeline());
    onWs("reconnected", () => {
      wake();
      void refreshPipeline();
    });
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    window.addEventListener("pageshow", wake);
    pump();
    void refreshPipeline();
  })();
  return initp;
}

export function resume(): void {
  pump();
}
