// QA-only simulation of the capture state machine (VITE_MOCK_CAPTURE=1 builds only).
// Emits the same WebSocket events the backend will: capture.state and cover.
import { ApiError } from "./client";
import { emitLocal } from "./ws";
import type { CaptureState, CaptureStatus } from "./types";

let cur: CaptureStatus = { state: "idle" };
let timers: ReturnType<typeof setTimeout>[] = [];
let n = 0;

const REVIEW_IMG =
  "data:image/svg+xml," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 400"><rect width="300" height="400" fill="#d9d4cc"/><circle cx="150" cy="105" r="45" fill="#b9a48e"/><path d="M60 400 L75 210 Q150 160 225 210 L240 400Z" fill="#33415c"/><text x="150" y="385" font-family="sans-serif" font-size="16" text-anchor="middle" fill="#fff">mock capture</text></svg>`,
  );

function set(state: CaptureState, extra: Partial<CaptureStatus> = {}) {
  cur = { capture_id: cur.capture_id, state, message: null, seconds_left: null, review_url: null, ...extra };
  emitLocal({ type: "capture.state", data: cur, at: new Date().toISOString() });
}
function later(ms: number, fn: () => void) {
  timers.push(setTimeout(fn, ms));
}
function clear() {
  timers.forEach(clearTimeout);
  timers = [];
}

function run() {
  clear();
  set("cover_wait", { message: "Slide the camera cover open" });
  later(1800, () => {
    emitLocal({ type: "cover", data: { open: true } });
    set("preview", { message: "Stand in the outline" });
  });
  later(3300, () => set("countdown", { seconds_left: 3 }));
  later(4300, () => set("countdown", { seconds_left: 2 }));
  later(5300, () => set("countdown", { seconds_left: 1 }));
  later(6300, () => set("flash"));
  later(6600, () => set("quality_check", { message: "Checking the photo" }));
  later(7800, () => set("review", { review_url: REVIEW_IMG, seconds_left: 60, message: "Keep or retake?" }));
}

export async function mockStart(): Promise<CaptureStatus> {
  if (cur.state !== "idle") throw new ApiError(409, "a capture is already running");
  cur = { capture_id: `mock-${++n}`, state: "cover_wait" };
  setTimeout(run, 50);
  return cur;
}
export async function mockCurrent(): Promise<CaptureStatus> {
  return cur;
}
export async function mockAction(action: "keep" | "retake" | "cancel"): Promise<CaptureStatus> {
  clear();
  if (action === "retake") {
    run();
    return cur;
  }
  set("cover_reminder", { message: "Close the camera cover" });
  later(4000, () => {
    emitLocal({ type: "cover", data: { open: false } });
    set("idle");
    cur = { state: "idle" };
  });
  return cur;
}
