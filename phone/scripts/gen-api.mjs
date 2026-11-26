// Generate src/api/schema.d.ts from the backend's OpenAPI document.
// Usage: API_URL=http://127.0.0.1:8806 npm run gen:api   (or OPENAPI_FILE=path/to/openapi.json)
import { execFileSync } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

let src = process.env.OPENAPI_FILE;
if (!src) {
  const base = process.env.API_URL || "http://127.0.0.1:8000";
  const res = await fetch(`${base}/openapi.json`);
  if (!res.ok) throw new Error(`GET ${base}/openapi.json -> ${res.status}`);
  src = join(mkdtempSync(join(tmpdir(), "myror-")), "openapi.json");
  writeFileSync(src, await res.text());
}
execFileSync("npx", ["openapi-typescript", src, "-o", "src/api/schema.d.ts"], { stdio: "inherit" });
