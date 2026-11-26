// Minimal IndexedDB key-value store for the upload queue (Safari stores Blobs in IndexedDB).
// Falls back to memory when IndexedDB is unavailable (some private-browsing modes).

const DB = "myror";
const STORE = "uploads";
let dbp: Promise<IDBDatabase | null> | null = null;
const mem = new Map<string, unknown>();

function open(): Promise<IDBDatabase | null> {
  if (dbp) return dbp;
  dbp = new Promise((resolve) => {
    try {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
  return dbp;
}

function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T | undefined> {
  return open().then(
    (db) =>
      new Promise((resolve) => {
        if (!db) return resolve(undefined);
        try {
          const t = db.transaction(STORE, mode);
          const r = fn(t.objectStore(STORE));
          r.onsuccess = () => resolve(r.result);
          r.onerror = () => resolve(undefined);
        } catch {
          resolve(undefined);
        }
      }),
  );
}

export async function idbPut(key: string, value: unknown): Promise<void> {
  mem.set(key, value);
  await tx("readwrite", (s) => s.put(value, key));
}
export async function idbDel(key: string): Promise<void> {
  mem.delete(key);
  await tx("readwrite", (s) => s.delete(key));
}
export async function idbAll<T>(): Promise<T[]> {
  const db = await open();
  if (!db) return [...mem.values()] as T[];
  return ((await tx("readonly", (s) => s.getAll())) as T[] | undefined) ?? ([...mem.values()] as T[]);
}
