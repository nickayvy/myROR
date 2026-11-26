// Capture routes (contract fixed by the admin; HANDOFF section 9):
//   POST /api/capture -> {capture_id, state} (409 if one is running)
//   GET  /api/capture/current -> CaptureStatus | {state: "idle"}
//   POST /api/capture/{id}/keep | /retake | /cancel
// Live state arrives as WebSocket `capture.state`, plus `cover` {open}.
//
// When the app is built with VITE_MOCK_CAPTURE=1 and the server answers 404 (routes not
// deployed yet), a local simulation of the state machine stands in, emitting the same
// events. Production builds leave the mock out entirely.
import { ApiError, api } from "./client";
import type { CaptureStatus } from "./types";

const MOCK = import.meta.env.VITE_MOCK_CAPTURE === "1";

async function withMock<T>(real: () => Promise<T>, mock: () => Promise<T>): Promise<T> {
  try {
    return await real();
  } catch (e) {
    if (MOCK && e instanceof ApiError && (e.status === 404 || e.status === 405)) return mock();
    throw e;
  }
}

async function mockModule() {
  return import("./mockCapture");
}

export function startCapture(): Promise<CaptureStatus> {
  return withMock(
    () => api<CaptureStatus>("/api/capture", { method: "POST" }),
    async () => (await mockModule()).mockStart(),
  );
}
export function currentCapture(): Promise<CaptureStatus> {
  return withMock(
    () => api<CaptureStatus>("/api/capture/current"),
    async () => (await mockModule()).mockCurrent(),
  );
}
export function captureAction(id: string, action: "keep" | "retake" | "cancel"): Promise<CaptureStatus | undefined> {
  return withMock(
    () => api<CaptureStatus | undefined>(`/api/capture/${encodeURIComponent(id)}/${action}`, { method: "POST" }),
    async () => (await mockModule()).mockAction(action),
  );
}

/** Send an unmatched check-in garment to the review inbox (201 first time, 200 repeat,
 * 409 when the garment matched an item you own). */
export function garmentToInbox(photoId: string, detectionId: string): Promise<unknown> {
  return api(`/api/checkins/${photoId}/garments/${detectionId}/inbox`, { method: "POST" });
}
